# Groww Trade API — Smart Orders (GTT + OCO)

> **Source:** https://groww.in/trade-api/docs/python-sdk/smart-orders
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Smart Orders

Automate entry and exit strategies with Smart Orders using GTT and OCO orders. This guide demonstrates creating, modifying, canceling, and managing smart orders for CASH and F&O segments.

Smart Orders help you automate entries/exits with minimal code. Two types are supported:

- **GTT (Good Till Triggered)**: Triggers a single order when price crosses your trigger
- **OCO (One Cancels the Other)**: Places target and stop-loss together; execution of one cancels the other

**Note:** The `COMMODITY` segment is not supported for Smart Orders.

## [Create GTT](#create-gtt)

Create a GTT that arms a single order when your trigger condition is met.

### [Python SDK Usage](#python-sdk-usage)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
gtt_response = groww.create_smart_order(
    smart_order_type=groww.SMART_ORDER_TYPE_GTT,
    reference_id="gtt-ref-unique123",
    segment=groww.SEGMENT_CASH,
    trading_symbol="TCS",
    quantity=10,
    product_type=groww.PRODUCT_CNC,
    exchange=groww.EXCHANGE_NSE,
    duration=groww.VALIDITY_DAY,
    # GTT-specific parameters
    trigger_price="3985.00",
    trigger_direction=groww.TRIGGER_DIRECTION_DOWN,
    order={
        "order_type": groww.ORDER_TYPE_LIMIT,
        "price": "3990.00",
        "transaction_type": groww.TRANSACTION_TYPE_BUY
    }
)
print(gtt_response)
```

### [Parameters](#parameters)

| Name | Type | Description |
| --- | --- | --- |
| smart\_order\_type `*` | str | Set to `GTT` to create a Good-Till-Triggered smart order. Example: `GTT`. |
| reference\_id `*` | str | User-provided alphanumeric string (8-20 characters) that serves as an idempotency key, with at most two hyphens (-) allowed. |
| segment `*` | str | Segment for which the order will be placed. See [Segment](/trade-api/docs/python-sdk/annexures#segment). Examples: `CASH`, `FNO`. |
| trading\_symbol `*` | str | Trading Symbol of the instrument as defined by the exchange |
| quantity `*` | int | Quantity for the post-trigger order. For FNO, must respect lot size. Examples: `10`, `50`. |
| trigger\_price `*` | str | Trigger price as a decimal string. Example: `"3985.00"`. |
| trigger\_direction `*` | str | Direction to monitor relative to the trigger price. Examples: `UP`, `DOWN`. |
| order `*` | dict | Post-trigger order details |
| order["order\_type"] `*` | str | Post-trigger execution order type. See [Order type](/trade-api/docs/python-sdk/annexures#order-type). Examples: `LIMIT`, `SL`. |
| order["price"] | str | Post-trigger limit price (required if `order_type` is `LIMIT` or `SL`). Example: `"3990.00"`. |
| order["transaction\_type"] `*` | str | Post-trigger transaction type. See [Transaction type](/trade-api/docs/python-sdk/annexures#transaction-type). Examples: `BUY`, `SELL`. |
| child\_legs | dict | Optional child legs for bracket orders (target/stop-loss). |
| product\_type `*` | str | Product for the post-trigger order. See [Product type](/trade-api/docs/python-sdk/annexures#product). Examples: `CNC`, `MIS`. |
| exchange `*` | str | Exchange where the instrument is traded. See [Exchange](/trade-api/docs/python-sdk/annexures#exchange). Examples: `NSE`. |
| duration `*` | str | Validity of the post-trigger order. See [Validity](/trade-api/docs/python-sdk/annexures#validity). Example: `DAY`. |

`*`required parameters

### [Response](#response)

```
{
  "smart_order_id": "gtt_91a7f4",
  "smart_order_type": "GTT",
  "status": "ACTIVE",
  "trading_symbol": "TCS",
  "exchange": "NSE",
  "quantity": 10,
  "product_type": "CNC",
  "duration": "DAY",
  "order": {
    "order_type": "LIMIT",
    "price": "3990.00",
    "transaction_type": "BUY"
  },
  "trigger_direction": "DOWN",
  "trigger_price": "3985.00",
  "segment": "CASH",
  "ltp": 4000.50,
  "remark": null,
  "display_name": "TCS Ltd",
  "child_legs": null,
  "is_cancellation_allowed": true,
  "is_modification_allowed": true,
  "created_at": "2025-09-30T07:00:00",
  "expire_at": "2026-09-30T07:00:00",
  "triggered_at": null,
  "updated_at": "2025-09-30T07:00:00"
}
```

Schema reference: [Smart Order Response (GTT)](#smart-order-response-gtt).

## [Create OCO](#create-oco)

Create an OCO to protect or exit positions with target and stop-loss.

### [Python SDK Usage](#python-sdk-usage-1)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
oco_response = groww.create_smart_order(
    smart_order_type=groww.SMART_ORDER_TYPE_OCO,
    reference_id="oco-ref-unique456",
    segment=groww.SEGMENT_FNO,
    trading_symbol="NIFTY25OCT24000CE",
    quantity=50,
    product_type=groww.PRODUCT_MIS,
    exchange=groww.EXCHANGE_NSE,
    duration=groww.VALIDITY_DAY,
    # OCO-specific parameters
    net_position_quantity=50,
    transaction_type=groww.TRANSACTION_TYPE_SELL,
    target={
        "trigger_price": "120.50",
        "order_type": groww.ORDER_TYPE_LIMIT,
        "price": "121.00"
    },
    stop_loss={
        "trigger_price": "95.00",
        "order_type": groww.ORDER_TYPE_STOP_LOSS_MARKET,
        "price": None
    }
)
print(oco_response)
```

### [Parameters](#parameters-1)

| Name | Type | Description |
| --- | --- | --- |
| smart\_order\_type `*` | str | Set to `OCO` to create a One-Cancels-Other smart order (target + stop-loss). Example: `OCO`. |
| reference\_id `*` | str | User-provided alphanumeric string (8-20 characters) that serves as an idempotency key, with at most two hyphens (-) allowed. |
| segment `*` | str | Segment for which the order will be placed. See [Segment](/trade-api/docs/python-sdk/annexures#segment). Examples: `FNO`, `CASH`. |
| trading\_symbol `*` | str | Trading Symbol of the instrument as defined by the exchange |
| quantity `*` | int | Total quantity for both legs. Must be ≤ `abs(net_position_quantity)`. Example: `50`. |
| net\_position\_quantity `*` | int | Your current net position in this symbol. Used to derive leg directions and validate quantity. Example: `50`. |
| transaction\_type `*` | str | Direction of protection/exit for your position. See [Transaction type](/trade-api/docs/python-sdk/annexures#transaction-type). Examples: `BUY`, `SELL`. |
| target `*` | dict | Take-profit leg details |
| target["trigger\_price"] `*` | str | Take-profit trigger price (decimal string). Example: `"120.50"`. |
| target["order\_type"] `*` | str | Order type for the target leg. See [Order type](/trade-api/docs/python-sdk/annexures#order-type). Examples: `LIMIT`, `MARKET`. |
| target["price"] | str | Target leg limit price (required if `order_type` = `LIMIT`). Example: `"121.00"`. |
| stop\_loss `*` | dict | Stop-loss leg details |
| stop\_loss["trigger\_price"] `*` | str | Stop-loss trigger price (decimal string). Example: `"95.00"`. |
| stop\_loss["order\_type"] `*` | str | Order type for the stop-loss leg. See [Order type](/trade-api/docs/python-sdk/annexures#order-type). Examples: `SL`, `SL_M`. |
| stop\_loss["price"] | str | Stop-loss leg limit price (required if `order_type` = `SL`). Example: `"94.50"`. |
| product\_type `*` | str | Product for the OCO. See [Product type](/trade-api/docs/python-sdk/annexures#product). Note: For OCO in cash segment, only `MIS` is supported currently. |
| exchange `*` | str | Exchange for this instrument. See [Exchange](/trade-api/docs/python-sdk/annexures#exchange). Example: `NSE`. |
| duration `*` | str | Validity for both legs. See [Validity](/trade-api/docs/python-sdk/annexures#validity). Example: `DAY`. |

`*`required parameters

> **Important**
> OCO orders are meant to exit an existing position.
>
> - In the **F&O** segment you must already hold the contract.
> - In the **CASH** segment, OCO is restricted to intraday (`MIS`) positions.

### [Response](#response-1)

```
{
  "smart_order_id": "oco_a12bc3",
  "smart_order_type": "OCO",
  "status": "ACTIVE",
  "trading_symbol": "NIFTY25OCT24000CE",
  "exchange": "NSE",
  "quantity": 50,
  "product_type": "MIS",
  "duration": "DAY",
  "target": {
    "trigger_price": "120.50",
    "order_type": "LIMIT",
    "price": "121.00"
  },
  "stop_loss": {
    "trigger_price": "95.00",
    "order_type": "SL_M",
    "price": null
  },
  "is_cancellation_allowed": true,
  "is_modification_allowed": true,
  "created_at": "2025-09-30T07:00:00",
  "expire_at": null,
  "triggered_at": null,
  "updated_at": "2025-09-30T07:00:00"
}
```

Schema reference: [Smart Order Response (OCO)](#smart-order-response-oco).

### [Notes](#notes)

- `quantity` must be ≤ `abs(net_position_quantity)`.
- If a leg executes, the other cancels automatically.

## [Modify Smart Order](#modify-smart-order)

Modify contracts differ by flow. Only the fields listed below are honoured; everything else is ignored or rejected.
Use cancel + create when you need changes outside of these lists.

### [Modifiable Fields - GTT](#modifiable-fields---gtt)

- `quantity` - Updated order quantity
- `trigger_price` - Updated trigger price threshold
- `trigger_direction` - Updated direction
- `order.order_type` - Updated order type
- `order.price` - Updated limit price (required for `LIMIT`/`SL` types; set to `None` for `MARKET`/`SL_M`)
- `child_legs` - Updated bracket order legs (optional; all child leg fields are modifiable if provided)

### [Modifiable Fields - OCO](#modifiable-fields---oco)

- `quantity` - Updated order quantity
- `duration` - Updated validity
- `product_type` - Updated product (e.g., MIS ↔ NRML for FNO; CASH OCO only supports MIS)
- `target.trigger_price` - Updated profit target trigger price
- `stop_loss.trigger_price` - Updated stop-loss trigger price

### [Modify GTT](#modify-gtt)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
modify_response = groww.modify_smart_order(
    smart_order_id="gtt_91a7f4",
    smart_order_type=groww.SMART_ORDER_TYPE_GTT,
    segment=groww.SEGMENT_CASH,
    quantity=12,
    trigger_price="3980.00",
    trigger_direction=groww.TRIGGER_DIRECTION_DOWN,
    order={
        "order_type": groww.ORDER_TYPE_LIMIT,
        "price": "3985.00",
        "transaction_type": groww.TRANSACTION_TYPE_BUY
    }
)
print(modify_response)
```

#### [Parameters (GTT)](#parameters-gtt)

| Name | Type | Description |
| --- | --- | --- |
| smart\_order\_id `*` | str | Smart order identifier. Example: `gtt_91a7f4`. |
| smart\_order\_type `*` | str | Set to `GTT` (required for routing) |
| segment `*` | str | Segment of the order (required for routing). See [Segment](/trade-api/docs/python-sdk/annexures#segment). Examples: `CASH`, `FNO`. |
| quantity | int | Updated quantity. |
| trigger\_price | str | Updated trigger price (decimal string). |
| trigger\_direction | str | Updated trigger direction. Examples: `UP`, `DOWN`. |
| order["order\_type"] | str | Updated order type. See [Order type](/trade-api/docs/python-sdk/annexures#order-type). |
| order["price"] | str | Updated limit price (required if `order_type` is `LIMIT` or `SL`; set to `None` for `MARKET`/`SL_M`). |
| order["transaction\_type"] | str | Transaction type (required but not modifiable). See [Transaction type](/trade-api/docs/python-sdk/annexures#transaction-type). |
| child\_legs | dict | Updated child legs for bracket orders (all child leg fields modifiable if provided). |

`*`required parameters

### [Modify OCO](#modify-oco)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
modify_oco_response = groww.modify_smart_order(
    smart_order_id="oco_a12bc3",
    smart_order_type=groww.SMART_ORDER_TYPE_OCO,
    segment=groww.SEGMENT_FNO,
    quantity=40,
    duration=groww.VALIDITY_DAY,
    product_type=groww.PRODUCT_MIS,
    target={
        "trigger_price": "122.00"
    },
    stop_loss={
        "trigger_price": "97.50"
    }
)
print(modify_oco_response)
```

#### [Parameters (OCO)](#parameters-oco)

| Name | Type | Description |
| --- | --- | --- |
| smart\_order\_id `*` | str | Smart order identifier. Example: `oco_a12bc3`. |
| smart\_order\_type `*` | str | Set to `OCO` (required for routing) |
| segment `*` | str | Segment of the order (required for routing). See [Segment](/trade-api/docs/python-sdk/annexures#segment). Examples: `FNO`, `CASH`. |
| duration | str | Updated validity. See [Validity](/trade-api/docs/python-sdk/annexures#validity). |
| quantity | int | Updated total quantity. |
| product\_type | str | Updated product. See [Product type](/trade-api/docs/python-sdk/annexures#product). |
| target["trigger\_price"] | str | Updated target trigger price (decimal string). |
| target["order\_type"] | str | Target order type (not modifiable). See [Order type](/trade-api/docs/python-sdk/annexures#order-type). |
| target["price"] | str | Target limit price (not modifiable). |
| stop\_loss["trigger\_price"] | str | Updated stop-loss trigger price (decimal string). |
| stop\_loss["order\_type"] | str | Stop-loss order type (not modifiable). See [Order type](/trade-api/docs/python-sdk/annexures#order-type). |
| stop\_loss["price"] | str | Stop-loss limit price (not modifiable). |

`*`required parameters

### [Response](#response-2)

```
{
  "smart_order_id": "oco_a12bc3",
  "smart_order_type": "OCO",
  "status": "ACTIVE",
  "quantity": 40
}
```

Schema reference: [Smart Order Response (GTT)](#smart-order-response-gtt) or [Smart Order Response (OCO)](#smart-order-response-oco).

### [Modify vs Cancel-Create](#modify-vs-cancel-create)

| What you need to change | GTT | OCO | Action |
| --- | --- | --- | --- |
| Quantity | ✅ Modify | ✅ Modify | Use modify |
| Trigger price | ✅ Modify | ✅ Modify (both legs) | Use modify |
| Trigger direction | ✅ Modify | N/A | Use modify |
| Order type | ✅ Modify | ❌ Not modifiable | GTT: modify; OCO: cancel+create |
| Limit price | ✅ Modify | ❌ Not modifiable | GTT: modify; OCO: cancel+create |
| Duration/validity | ❌ Not modifiable | ✅ Modify | OCO: modify; GTT: cancel+create |
| Product type | ❌ Not modifiable | ✅ Modify | OCO: modify; GTT: cancel+create |
| Symbol/contract | ❌ Not modifiable | ❌ Not modifiable | Cancel + create |
| Exchange | ❌ Not modifiable | ❌ Not modifiable | Cancel + create |
| Segment | ❌ Not modifiable | ❌ Not modifiable | Cancel + create |
| Smart order type (GTT ↔ OCO) | ❌ Not modifiable | ❌ Not modifiable | Cancel + create |

> **Note:** When a field is marked as "Not modifiable" (❌), you must cancel the existing smart order and create a new one with the desired changes. The "N/A" designation indicates that the feature does not apply to that smart order type.

## [Cancel Smart Order](#cancel-smart-order)

Cancel any active smart order.

### [Python SDK Usage](#python-sdk-usage-2)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
cancel_response = groww.cancel_smart_order(
    segment=groww.SEGMENT_CASH,
    smart_order_type=groww.SMART_ORDER_TYPE_GTT,
    smart_order_id="gtt_91a7f4"
)
print(cancel_response)
```

### [Parameters](#parameters-2)

| Name | Type | Description |
| --- | --- | --- |
| segment `*` | str | Segment of the smart order to cancel. See [Segment](/trade-api/docs/python-sdk/annexures#segment). Examples: `CASH`, `FNO`. |
| smart\_order\_type `*` | str | Smart order type. Examples: `GTT`, `OCO`. |
| smart\_order\_id `*` | str | Smart order identifier. Example: `gtt_91a7f4`. |

`*`required parameters

### [Response](#response-3)

```
{
  "smart_order_id": "gtt_91a7f4",
  "smart_order_type": "GTT",
  "status": "CANCELLED"
}
```

Schema reference: [Smart Order Response (GTT)](#smart-order-response-gtt) or [Smart Order Response (OCO)](#smart-order-response-oco).

## [Get Smart Order](#get-smart-order)

Retrieve details of a specific smart order by its internal ID.

### [Python SDK Usage](#python-sdk-usage-3)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
order_details = groww.get_smart_order(
    segment=groww.SEGMENT_CASH,
    smart_order_type=groww.SMART_ORDER_TYPE_GTT,
    smart_order_id="gtt_91a7f4"
)
print(order_details)
```

### [Parameters](#parameters-3)

| Name | Type | Description |
| --- | --- | --- |
| segment `*` | str | Segment of the smart order to fetch. See [Segment](/trade-api/docs/python-sdk/annexures#segment). Examples: `CASH`, `FNO`. |
| smart\_order\_type `*` | str | Smart order type. Examples: `GTT`, `OCO`. |
| smart\_order\_id `*` | str | Smart order identifier. Example: `gtt_91a7f4`. |

`*`required parameters

### [Response](#response-4)

```
{
  "smart_order_id": "gtt_91a7f4",
  "smart_order_type": "GTT",
  "status": "ACTIVE",
  "trading_symbol": "TCS",
  "exchange": "NSE",
  "quantity": 10,
  "product_type": "CNC",
  "duration": "DAY",
  "order": {
    "order_type": "LIMIT",
    "price": "3990.00",
    "transaction_type": "BUY"
  },
  "trigger_direction": "DOWN",
  "trigger_price": "3985.00",
  "segment": "CASH",
  "ltp": 4000.50,
  "remark": null,
  "display_name": "TCS Ltd",
  "child_legs": null,
  "is_cancellation_allowed": true,
  "is_modification_allowed": true,
  "created_at": "2025-09-30T07:00:00",
  "expire_at": "2026-09-30T07:00:00",
  "triggered_at": null,
  "updated_at": "2025-09-30T07:00:00"
}
```

Schema reference: [Smart Order Response (GTT)](#smart-order-response-gtt) or [Smart Order Response (OCO)](#smart-order-response-oco).

## [List Smart Orders](#list-smart-orders)

Filter and list smart orders by type, segment, status and a time window. Pagination is supported.

### [Python SDK Usage](#python-sdk-usage-4)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
orders_list = groww.get_smart_order_list(
    segment=groww.SEGMENT_FNO,
    smart_order_type=groww.SMART_ORDER_TYPE_OCO,
    status=groww.SMART_ORDER_STATUS_ACTIVE,
    page=0,
    page_size=10,
    start_date_time="2025-01-16T09:15:00",
    end_date_time="2025-01-16T15:30:00"
)
print(orders_list)
```

### [Parameters](#parameters-4)

| Name | Type | Description |
| --- | --- | --- |
| segment | str | Segment to list smart orders for. See [Segment](/trade-api/docs/python-sdk/annexures#segment). Examples: `FNO`, `CASH`. Optional; server defaults may apply if omitted. |
| smart\_order\_type | str | Smart order type to list. Examples: `OCO`, `GTT`. Optional; server defaults may apply if omitted. |
| status | str | Current state filter. Examples: `ACTIVE`, `TRIGGERED`, `CANCELLED`, `COMPLETED`. Optional; server defaults may apply if omitted. |
| page | int | Page number starting from 0. Use with `page_size` to paginate long lists. Min: `0`, Max: `500`. |
| page\_size | int | Number of records per page. Tune this for your UI/export. Min: `1`, Max: `50`. |
| start\_date\_time | str | Inclusive start of the time window in ISO 8601. Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |
| end\_date\_time | str | Inclusive end of the time window in ISO 8601. Must not be before `start_date_time`. Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |

Validations:

- `end_date_time` must not be before `start_date_time`.
- Date range between `start_date_time` and `end_date_time` must not exceed one month.

> **Tip**
> If you expect an order that has already been triggered or cancelled, switch the `status` filter accordingly—`ACTIVE` only returns live, untriggered smart orders.

### [Response](#response-5)

```
{
  "orders": [
    {
      "smart_order_id": "gtt_91a7f4",
      "smart_order_type": "GTT",
      "status": "ACTIVE",
      "trading_symbol": "TCS",
      "exchange": "NSE",
      "quantity": 10
    }
  ]
}
```

| Name | Type | Description |
| --- | --- | --- |
| orders | list | List of smart orders |
| orders[] | dict | Smart order item (GTT or OCO). See [Smart Order Response (GTT)](#smart-order-response-gtt) or [Smart Order Response (OCO)](#smart-order-response-oco) |

## [Smart Order Constants](#smart-order-constants)

The Python SDK provides constants for smart order types, trigger directions, and statuses:

### [Smart Order Types](#smart-order-types)

```
groww.SMART_ORDER_TYPE_GTT  # "GTT"
groww.SMART_ORDER_TYPE_OCO  # "OCO"
```

### [Trigger Directions](#trigger-directions)

```
groww.TRIGGER_DIRECTION_UP    # "UP"
groww.TRIGGER_DIRECTION_DOWN  # "DOWN"
```

### [Smart Order Status](#smart-order-status)

```
groww.SMART_ORDER_STATUS_ACTIVE      # "ACTIVE" - Order is monitoring trigger conditions
groww.SMART_ORDER_STATUS_TRIGGERED   # "TRIGGERED" - Trigger condition met, order placed
groww.SMART_ORDER_STATUS_CANCELLED   # "CANCELLED" - User cancelled the order
groww.SMART_ORDER_STATUS_EXPIRED     # "EXPIRED" - Order expired due to time/date expiry
groww.SMART_ORDER_STATUS_FAILED      # "FAILED" - Order placement or trigger failed
groww.SMART_ORDER_STATUS_COMPLETED   # "COMPLETED" - Order successfully completed
```

## [Quick Tips](#quick-tips)

- Use a unique `reference_id` per new smart order to avoid accidental duplicates.
- For OCO, ensure `quantity` ≤ `abs(net_position_quantity)`. The leg directions are derived from your net position.
- All prices should be passed as decimal strings (e.g., `"3985.00"`).
- Use the SDK constants (like `SMART_ORDER_TYPE_GTT`) for better type safety and code clarity.

## [Schemas](#schemas)

### [Smart Order Response (GTT)](#smart-order-response-gtt)

| Name | Type | Description |
| --- | --- | --- |
| smart\_order\_id | str | Smart order identifier |
| smart\_order\_type | str | Smart order type. Example: `GTT`. |
| status | str | Smart order status (e.g., `ACTIVE`) |
| trading\_symbol | str | Trading Symbol of the instrument as defined by the exchange |
| exchange | str | [Stock exchange](/trade-api/docs/python-sdk/annexures#exchange) |
| quantity | int | Quantity |
| product\_type | str | [Product type](/trade-api/docs/python-sdk/annexures#product) |
| duration | str | [Validity](/trade-api/docs/python-sdk/annexures#validity) |
| order.order\_type | str | Order type. See [Order type](/trade-api/docs/python-sdk/annexures#order-type). Examples: `LIMIT`, `SL`. |
| order.price | str | Price for LIMIT/SL |
| order.transaction\_type | str | See [Transaction type](/trade-api/docs/python-sdk/annexures#transaction-type). Examples: `BUY`, `SELL`. |
| ltp | float | Last traded price of the instrument |
| trigger\_direction | str | Trigger direction. Examples: `UP`, `DOWN`. |
| trigger\_price | str | Trigger price (decimal string) |
| segment | str | Market segment |
| remark | str | Remark or status message |
| display\_name | str | Display name for the instrument |
| child\_legs | dict | Child legs for bracket orders (target/stop-loss) |
| is\_cancellation\_allowed | bool | Whether cancellation is allowed |
| is\_modification\_allowed | bool | Whether modification is allowed |
| created\_at | str | Creation time (ISO 8601). Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |
| expire\_at | str | Expiry time (ISO 8601). Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |
| triggered\_at | str | Trigger time (ISO 8601). Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |
| updated\_at | str | Last updated time (ISO 8601). Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |

### [Smart Order Response (OCO)](#smart-order-response-oco)

| Name | Type | Description |
| --- | --- | --- |
| smart\_order\_id | str | Smart order identifier |
| smart\_order\_type | str | Smart order type. Example: `OCO`. |
| status | str | Smart order status (e.g., `ACTIVE`) |
| trading\_symbol | str | Trading Symbol of the instrument as defined by the exchange |
| exchange | str | [Stock exchange](/trade-api/docs/python-sdk/annexures#exchange) |
| quantity | int | Quantity of the order to be placed |
| product\_type | str | [Product type](/trade-api/docs/python-sdk/annexures#product) |
| duration | str | [Validity](/trade-api/docs/python-sdk/annexures#validity) |
| target.trigger\_price | str | Target trigger price (decimal string) |
| target.order\_type | str | Order type. See [Order type](/trade-api/docs/python-sdk/annexures#order-type). Examples: `LIMIT`, `MARKET`. |
| target.price | str | Target limit price |
| stop\_loss.trigger\_price | str | Stop-loss trigger price (decimal string) |
| stop\_loss.order\_type | str | Order type. See [Order type](/trade-api/docs/python-sdk/annexures#order-type). Examples: `SL`, `SL_M`. |
| stop\_loss.price | str | Stop-loss limit price |
| is\_cancellation\_allowed | bool | Whether cancellation is allowed |
| is\_modification\_allowed | bool | Whether modification is allowed |
| created\_at | str | Creation time (ISO 8601). Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |
| expire\_at | str | Expiry time (ISO 8601). Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |
| triggered\_at | str | Trigger time (ISO 8601). Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |
| updated\_at | str | Last updated time (ISO 8601). Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |

[Previous

Orders](/trade-api/docs/python-sdk/orders)[Next

Portfolio](/trade-api/docs/python-sdk/portfolio)
