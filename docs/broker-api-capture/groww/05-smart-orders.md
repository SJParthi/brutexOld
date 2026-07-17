# Groww Trading API — Smart Orders (GTT & OCO)

> Sources:
> - https://groww.in/trade-api/docs/curl/smart-orders
> - https://groww.in/trade-api/docs/python-sdk/smart-orders
> Captured: 2026-07-15

Smart Orders help you automate entries/exits with minimal code. Two types are supported today:

- **GTT (Good Till Triggered)**: Triggers a single order when price crosses your trigger
- **OCO (One Cancels the Other)**: Places target and stop-loss together; execution of one cancels the other

**Note (cURL docs):** The `COMMODITY` segment is not supported for Smart Orders. OCO orders for `CASH` segment are currently not supported.
**Note (Python SDK docs):** The `COMMODITY` segment is not supported for Smart Orders. (Its OCO section additionally says: in the CASH segment, OCO is restricted to intraday `MIS` positions.)

Endpoints summary:

| Operation | Method + Path | Python SDK method |
| --- | --- | --- |
| Create GTT/OCO | `POST /v1/order-advance/create` | `groww.create_smart_order(...)` |
| Modify Smart Order | `PUT /v1/order-advance/modify/{smart_order_id}` | `groww.modify_smart_order(...)` |
| Cancel Smart Order | `POST /v1/order-advance/cancel/{segment}/{smart_order_type}/{smart_order_id}` | `groww.cancel_smart_order(...)` |
| Get Smart Order | `GET /v1/order-advance/status/{segment}/{smart_order_type}/internal/{smart_order_id}` | `groww.get_smart_order(...)` |
| List Smart Orders | `GET /v1/order-advance/list` | `groww.get_smart_order_list(...)` |

---

## Create GTT

`POST https://api.groww.in/v1/order-advance/create`

Create a GTT that arms a single order when your trigger condition is met.

### cURL Request

```bash
curl -X POST https://api.groww.in/v1/order-advance/create \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

Body parameter:

```json
{
  "reference_id": "sref-unique-123",
  "smart_order_type": "GTT",
  "segment": "CASH",
  "trading_symbol": "TCS",
  "quantity": 10,
  "trigger_price": "3985.00",
  "trigger_direction": "DOWN",
  "order": {"order_type": "LIMIT", "price": "3990.00", "transaction_type": "BUY"},
  "product_type": "CNC",
  "exchange": "NSE",
  "duration": "DAY"
}
```

### Python SDK Usage

```python
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

### Request schema (GTT create)

| Name | Type | Description |
| --- | --- | --- |
| reference_id `*` | string | User-provided alphanumeric string (8-20 characters) that serves as an idempotency key, with at most two hyphens (-) allowed. |
| smart_order_type `*` | string | Set to `GTT` to create a Good-Till-Triggered smart order. Example: `GTT`. |
| segment `*` | string | Segment for which the order will be placed. See [Segment](./13-annexures.md#segment). Examples: `CASH`, `FNO`. |
| trading_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |
| quantity `*` | integer | Quantity for the post-trigger order. For FNO, must respect lot size. Examples: `10`, `50`. |
| trigger_price `*` | string | Trigger price as a decimal string. Example: `3985.00`. |
| trigger_direction `*` | string | Direction to monitor relative to the trigger price. Examples: `UP`, `DOWN`. |
| order.order_type `*` | string | Post-trigger execution order type. See [Order type](./13-annexures.md#order-type). Examples: `LIMIT`, `SL`. |
| order.price | string | Post-trigger limit price (required if `order.order_type` is `LIMIT` or `SL`). Example: `3990.00`. |
| order.transaction_type `*` | string | Post-trigger transaction type. See [Transaction type](./13-annexures.md#transaction-type). Examples: `BUY`, `SELL`. |
| child_legs | object/dict | Optional child legs for bracket orders (target/stop-loss). |
| product_type `*` | string | Product for the post-trigger order. See [Product type](./13-annexures.md#product). Examples: `CNC`, `MIS`. |
| exchange `*` | string | Exchange where the instrument is traded. See [Exchange](./13-annexures.md#exchange). Examples: `NSE`. |
| duration `*` | string | Validity of the post-trigger order. See [Validity](./13-annexures.md#validity). Example: `DAY`. |

`*` required parameters

### Response (201)

REST (wrapped):

```json
{
  "status": "SUCCESS",
  "payload": {
    "smart_order_id": "gtt_91a7f4",
    "smart_order_type": "GTT",
    "status": "ACTIVE",
    "trading_symbol": "TCS",
    "exchange": "NSE",
    "quantity": 10,
    "product_type": "CNC",
    "duration": "DAY",
    "order": {"order_type": "LIMIT", "price": "3990.00", "transaction_type": "BUY"},
    "trigger_direction": "DOWN",
    "trigger_price": "3985.00",
    "is_cancellation_allowed": true,
    "is_modification_allowed": true,
    "created_at": "2025-09-30T07:00:00",
    "expire_at": "2026-09-30T07:00:00",
    "triggered_at": null,
    "updated_at": "2025-09-30T07:00:00"
  }
}
```

Python SDK (payload only, richer example):

```json
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

---

## Create OCO

`POST https://api.groww.in/v1/order-advance/create`

Create an OCO to protect or exit positions with target and stop-loss.

> **Important:** OCO orders are meant to exit an existing position.
> - In the **F&O** segment you must already hold the contract.
> - In the **CASH** segment, OCO is restricted to intraday (`MIS`) positions.

### cURL Request

```bash
curl -X POST https://api.groww.in/v1/order-advance/create \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

Body parameter:

```json
{
  "reference_id": "sref-unique-456",
  "smart_order_type": "OCO",
  "segment": "FNO",
  "trading_symbol": "NIFTY25OCT24000CE",
  "quantity": 50,
  "net_position_quantity": 50,
  "transaction_type": "SELL",
  "target": {"trigger_price": "120.50", "order_type": "LIMIT", "price": "121.00"},
  "stop_loss": {"trigger_price": "95.00", "order_type": "SL_M", "price": null},
  "product_type": "MIS",
  "exchange": "NSE",
  "duration": "DAY"
}
```

### Python SDK Usage

```python
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

### Request schema (OCO create)

| Name | Type | Description |
| --- | --- | --- |
| reference_id `*` | string | User-provided alphanumeric string (8-20 characters) that serves as an idempotency key, with at most two hyphens (-) allowed. |
| smart_order_type `*` | string | Set to `OCO` to create a One-Cancels-Other smart order (target + stop-loss). Example: `OCO`. |
| segment `*` | string | Segment for which the order will be placed. See [Segment](./13-annexures.md#segment). Examples: `FNO`, `CASH`. |
| trading_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |
| quantity `*` | integer | Total quantity for both legs. Must be ≤ `abs(net_position_quantity)`. Example: `50`. |
| net_position_quantity `*` | integer | Your current net position in this symbol. Used to derive leg directions and validate quantity. Example: `50`. |
| transaction_type `*` | string | Direction of protection/exit for your position. See [Transaction type](./13-annexures.md#transaction-type). Examples: `BUY`, `SELL`. |
| target.trigger_price `*` | string | Take-profit trigger price (decimal string). Example: `120.50`. |
| target.order_type `*` | string | Order type for the target leg. See [Order type](./13-annexures.md#order-type). Examples: `LIMIT`, `MARKET`. |
| target.price | string | Target leg limit price (required if `target.order_type` = `LIMIT`). Example: `121.00`. |
| stop_loss.trigger_price `*` | string | Stop-loss trigger price (decimal string). Example: `95.00`. |
| stop_loss.order_type `*` | string | Order type for the stop-loss leg. See [Order type](./13-annexures.md#order-type). Examples: `SL`, `SL_M`. |
| stop_loss.price | string | Stop-loss leg limit price (required if `stop_loss.order_type` = `SL`). Example: `94.50`. |
| product_type `*` | string | Product for the OCO. See [Product type](./13-annexures.md#product). Note: For OCO in cash segment, only `MIS` is supported currently. |
| exchange `*` | string | Exchange for this instrument. See [Exchange](./13-annexures.md#exchange). Example: `NSE`. |
| duration `*` | string | Validity for both legs. See [Validity](./13-annexures.md#validity). Example: `DAY`. |

`*` required parameters

### Response (201)

```json
{
  "status": "SUCCESS",
  "payload": {
    "smart_order_id": "oco_a12bc3",
    "smart_order_type": "OCO",
    "status": "ACTIVE",
    "trading_symbol": "NIFTY25OCT24000CE",
    "exchange": "NSE",
    "quantity": 50,
    "product_type": "MIS",
    "duration": "DAY",
    "target": {"trigger_price": "120.50", "order_type": "LIMIT", "price": "121.00"},
    "stop_loss": {"trigger_price": "95.00", "order_type": "SL_M", "price": null},
    "created_at": "2025-09-30T07:00:00",
    "expire_at": null,
    "triggered_at": null,
    "updated_at": "2025-09-30T07:00:00"
  }
}
```

(The Python SDK response additionally includes `is_cancellation_allowed` / `is_modification_allowed` booleans.)

### Notes

- `quantity` must be ≤ `abs(net_position_quantity)`.
- If a leg executes, the other cancels automatically.

---

## Modify Smart Order

`PUT https://api.groww.in/v1/order-advance/modify/{smart_order_id}`

Modify contracts differ by flow. Only the fields listed below are honoured; everything else is ignored or rejected. Use cancel + create when you need changes outside of these lists.

### Modifiable Fields - GTT

- `quantity` - Updated order quantity
- `trigger_price` - Updated trigger price threshold
- `trigger_direction` - Updated direction
- `order.order_type` - Updated order type
- `order.price` - Updated limit price (required for `LIMIT`/`SL` types; set to `null`/`None` for `MARKET`/`SL_M`)
- `child_legs` - Updated bracket order legs (optional; all child leg fields are modifiable if provided)

### Modifiable Fields - OCO

- `quantity` - Updated order quantity
- `duration` - Updated validity
- `product_type` - Updated product (e.g., MIS ↔ NRML for FNO; CASH OCO only supports MIS)
- `target.trigger_price` - Updated profit target trigger price
- `stop_loss.trigger_price` - Updated stop-loss trigger price

### cURL Request (GTT)

```bash
curl -X PUT https://api.groww.in/v1/order-advance/modify/gtt_91a7f4 \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

Body parameter (GTT):

```json
{
  "smart_order_type": "GTT",
  "segment": "CASH",
  "quantity": 12,
  "trigger_price": "3980.00",
  "trigger_direction": "DOWN",
  "order": {"order_type": "LIMIT", "price": "3985.00", "transaction_type": "BUY"}
}
```

### Python SDK Usage (Modify GTT)

```python
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

### Request schema (Modify GTT)

| Name | Type | Description |
| --- | --- | --- |
| smart_order_id `*` (path/SDK arg) | string | Smart order identifier. Example: `gtt_91a7f4`. |
| smart_order_type `*` | string | Set to `GTT` to modify a Good-Till-Triggered smart order (required for routing). |
| segment `*` | string | Segment of the order (for routing). See [Segment](./13-annexures.md#segment). Examples: `CASH`, `FNO`. |
| quantity | integer | Updated quantity. |
| trigger_price | string | Updated trigger price (decimal string). |
| trigger_direction | string | Updated trigger direction. Examples: `UP`, `DOWN`. |
| order.order_type | string | Updated order type. See [Order type](./13-annexures.md#order-type). |
| order.price | string | Updated limit price (required if `order.order_type` is `LIMIT` or `SL`; set to `null`/`None` for `MARKET`/`SL_M`). |
| order.transaction_type | string | Transaction type (required but not modifiable). See [Transaction type](./13-annexures.md#transaction-type). |
| child_legs | object/dict | Updated child legs for bracket orders (target/stop-loss; all child leg fields modifiable if provided). |

`*` required parameters

### cURL Request (OCO)

```bash
curl -X PUT https://api.groww.in/v1/order-advance/modify/oco_a12bc3 \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

Body parameter (OCO):

```json
{
  "smart_order_type": "OCO",
  "segment": "FNO",
  "duration": "DAY",
  "quantity": 40,
  "product_type": "MIS",
  "target": { "trigger_price": "122.00" },
  "stop_loss": { "trigger_price": "97.50" }
}
```

### Python SDK Usage (Modify OCO)

```python
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

### Request schema (Modify OCO)

| Name | Type | Description |
| --- | --- | --- |
| smart_order_id `*` (path/SDK arg) | string | Smart order identifier. Example: `oco_a12bc3`. |
| smart_order_type `*` | string | Set to `OCO` to modify a One-Cancels-Other smart order (required for routing). |
| segment `*` | string | Segment of the order (for routing). See [Segment](./13-annexures.md#segment). Examples: `FNO`, `CASH`. |
| duration | string | Updated validity. See [Validity](./13-annexures.md#validity). Modifiable for OCO. |
| quantity | integer | Updated total quantity. |
| product_type | string | Updated product. See [Product type](./13-annexures.md#product). Modifiable for OCO. |
| target.trigger_price | string | Updated target trigger price (decimal string). |
| target.order_type | string | Target order type (not modifiable). |
| target.price | string | Target limit price (not modifiable). |
| stop_loss.trigger_price | string | Updated stop-loss trigger price (decimal string). |
| stop_loss.order_type | string | Stop-loss order type (not modifiable). |
| stop_loss.price | string | Stop-loss limit price (not modifiable). |

`*` required parameters

### Response (202)

```json
{
  "status": "SUCCESS",
  "payload": { "smart_order_id": "oco_a12bc3", "smart_order_type": "OCO", "status": "ACTIVE", "quantity": 40 }
}
```

### Modify vs Cancel-Create

| What you need to change | GTT | OCO | Action |
| ---------------------------- | ---------------- | -------------------- | ------------------------------- |
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

---

## Cancel Smart Order

`POST https://api.groww.in/v1/order-advance/cancel/{segment}/{smart_order_type}/{smart_order_id}`

Cancel any active smart order.

### cURL Request

```bash
curl -X POST https://api.groww.in/v1/order-advance/cancel/CASH/GTT/gtt_91a7f4 \
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

cancel_response = groww.cancel_smart_order(
    segment=groww.SEGMENT_CASH,
    smart_order_type=groww.SMART_ORDER_TYPE_GTT,
    smart_order_id="gtt_91a7f4"
)
print(cancel_response)
```

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| segment `*` | string | Segment of the smart order to cancel. See [Segment](./13-annexures.md#segment). Examples: `CASH`, `FNO`. |
| smart_order_type `*` | string | Smart order type. Examples: `GTT`, `OCO`. |
| smart_order_id `*` | string | Smart order identifier. Example: `gtt_91a7f4`. |

`*` required parameters

### Response (202)

```json
{
  "status": "SUCCESS",
  "payload": { "smart_order_id": "gtt_91a7f4", "smart_order_type": "GTT", "status": "CANCELLED" }
}
```

Response schema:

| Name | Type | Description |
| ------------------ | ------ | ---------------------------------------------------- |
| status | string | SUCCESS if processed successfully, FAILURE otherwise |
| smart_order_id | string | Smart order identifier |
| smart_order_type | string | Smart order type. Examples: `GTT`, `OCO`. |
| status (payload) | string | `CANCELLED` on success |

---

## Get Smart Order

`GET https://api.groww.in/v1/order-advance/status/{segment}/{smart_order_type}/internal/{smart_order_id}`

Retrieve details of a specific smart order by its internal ID.

### cURL Request

```bash
curl -X GET \
  'https://api.groww.in/v1/order-advance/status/CASH/GTT/internal/gtt_91a7f4' \
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

order_details = groww.get_smart_order(
    segment=groww.SEGMENT_CASH,
    smart_order_type=groww.SMART_ORDER_TYPE_GTT,
    smart_order_id="gtt_91a7f4"
)
print(order_details)
```

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| segment `*` | string | Segment of the smart order to fetch. See [Segment](./13-annexures.md#segment). Examples: `CASH`, `FNO`. |
| smart_order_type `*` | string | Smart order type. Examples: `GTT`, `OCO`. |
| smart_order_id `*` | string | Smart order identifier. Example: `gtt_91a7f4`. |

`*` required parameters

### Response (200)

Full smart order object (see "Smart Order Response (GTT/OCO)" schemas below). Python SDK example:

```json
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

---

## List Smart Orders

`GET https://api.groww.in/v1/order-advance/list`

Filter by type, segment, status and a time window. Pagination is supported.

### cURL Request

```bash
curl -X GET \
  'https://api.groww.in/v1/order-advance/list?segment=FNO&smart_order_type=OCO&status=ACTIVE&page=0&page_size=10&start_date_time=2025-01-16T09:15:00&end_date_time=2025-01-16T15:30:00' \
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

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| segment | string | Segment to list smart orders for. See [Segment](./13-annexures.md#segment). Examples: `FNO`, `CASH`. Optional; server defaults may apply if omitted. |
| smart_order_type | string | Smart order type to list. Examples: `OCO`, `GTT`. Default: `OCO`. |
| status | string | Current state filter (live vs past). Examples: `ACTIVE`, `TRIGGERED`, `CANCELLED`, `COMPLETED`. Default: `ACTIVE`. |
| page | integer | Page number starting from 0. Use with `page_size` to paginate long lists. Min: `0`, Max: `500`. Default: `0`. |
| page_size | integer | Number of records per page. Tune this for your UI/export. Min: `1`, Max: `50`. Default: `10`. |
| start_date_time | string | Inclusive start of the time window in ISO8601 (`YYYY-MM-DDThh:mm:ss`). Defaults to start of today (server timezone). Examples: `2025-01-16T09:15:00`, `2025-01-16T00:00:00`. |
| end_date_time | string | Inclusive end of the time window in ISO8601. Must not be before `start_date_time`. Defaults to start of next day (server timezone). Examples: `2025-01-16T15:30:00`, `2025-01-16T23:59:59`. |

Validations:

- `end_date_time` must not be before `start_date_time`.
- Date range between `start_date_time` and `end_date_time` must not exceed one month.

> **Tip:** If you expect an order that has already been triggered or cancelled, switch the `status` filter accordingly — `ACTIVE` only returns live, untriggered smart orders.

### Response (200)

```json
{
  "status": "SUCCESS",
  "payload": {
    "orders": [
      { "smart_order_id": "gtt_91a7f4", "smart_order_type": "GTT", "status": "ACTIVE" }
    ]
  }
}
```

Python SDK example (payload only):

```json
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
| -------- | ------ | --- |
| status | string | SUCCESS if processed successfully, FAILURE otherwise (REST wrapper only) |
| orders | array | List of smart orders |
| orders[] | object | Smart order item (GTT or OCO). See schemas below. |

---

## Smart Order Constants (Python SDK)

### Smart Order Types

```python
groww.SMART_ORDER_TYPE_GTT  # "GTT"
groww.SMART_ORDER_TYPE_OCO  # "OCO"
```

### Trigger Directions

```python
groww.TRIGGER_DIRECTION_UP    # "UP"
groww.TRIGGER_DIRECTION_DOWN  # "DOWN"
```

### Smart Order Status

```python
groww.SMART_ORDER_STATUS_ACTIVE      # "ACTIVE" - Order is monitoring trigger conditions
groww.SMART_ORDER_STATUS_TRIGGERED   # "TRIGGERED" - Trigger condition met, order placed
groww.SMART_ORDER_STATUS_CANCELLED   # "CANCELLED" - User cancelled the order
groww.SMART_ORDER_STATUS_EXPIRED     # "EXPIRED" - Order expired due to time/date expiry
groww.SMART_ORDER_STATUS_FAILED      # "FAILED" - Order placement or trigger failed
groww.SMART_ORDER_STATUS_COMPLETED   # "COMPLETED" - Order successfully completed
```

## Quick Tips

- Use a unique `reference_id` per new smart order to avoid accidental duplicates.
- For symbol/segment/type changes: cancel the old smart order and create a new one.
- For OCO, ensure `quantity` ≤ `abs(net_position_quantity)`. The leg directions are derived from your net position.
- All prices should be passed as decimal strings (e.g., `"3985.00"`).
- Use the SDK constants (like `SMART_ORDER_TYPE_GTT`) for better type safety and code clarity.

---

## Schemas

### Smart Order Response (GTT)

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if processed successfully, FAILURE otherwise (REST wrapper only) |
| smart_order_id | string | Smart order identifier |
| smart_order_type | string | Smart order type. Example: `GTT`. |
| status (payload) | string | Smart order status (e.g., `ACTIVE`) |
| trading_symbol | string | Trading Symbol of the instrument as defined by the exchange |
| exchange | string | [Stock exchange](./13-annexures.md#exchange) |
| quantity | integer | Quantity |
| product_type | string | [Product type](./13-annexures.md#product) |
| duration | string | [Validity](./13-annexures.md#validity) |
| order.order_type | string | Order type. See [Order type](./13-annexures.md#order-type). Examples: `LIMIT`, `SL`. |
| order.price | string | Price for LIMIT/SL |
| order.transaction_type | string | See [Transaction type](./13-annexures.md#transaction-type). Examples: `BUY`, `SELL`. |
| ltp | number (float) | Last traded price of the instrument |
| trigger_direction | string | Trigger direction. Examples: `UP`, `DOWN`. |
| trigger_price | string | Trigger price (decimal string) |
| segment | string | Market segment |
| remark | string | Remark or status message |
| display_name | string | Display name for the instrument |
| child_legs | object/dict | Child legs for bracket orders (target/stop-loss) |
| is_cancellation_allowed | boolean | Whether cancellation is allowed |
| is_modification_allowed | boolean | Whether modification is allowed |
| created_at | string | Creation time (ISO 8601) |
| expire_at | string | Expiry time (ISO 8601) |
| triggered_at | string | Trigger time (ISO 8601) |
| updated_at | string | Last updated time (ISO 8601) |

### Smart Order Response (OCO)

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if processed successfully, FAILURE otherwise (REST wrapper only) |
| smart_order_id | string | Smart order identifier |
| smart_order_type | string | Smart order type. Example: `OCO`. |
| status (payload) | string | Smart order status (e.g., `ACTIVE`) |
| trading_symbol | string | Trading Symbol of the instrument as defined by the exchange |
| exchange | string | [Stock exchange](./13-annexures.md#exchange) |
| quantity | integer | Quantity of the order to be placed |
| product_type | string | [Product type](./13-annexures.md#product) |
| duration | string | [Validity](./13-annexures.md#validity) |
| target.trigger_price | string | Target trigger price (decimal string) |
| target.order_type | string | Order type. Examples: `LIMIT`, `MARKET`. |
| target.price | string | Target limit price |
| stop_loss.trigger_price | string | Stop-loss trigger price (decimal string) |
| stop_loss.order_type | string | Order type. Examples: `SL`, `SL_M`. |
| stop_loss.price | string | Stop-loss limit price |
| is_cancellation_allowed | boolean | Whether cancellation is allowed |
| is_modification_allowed | boolean | Whether modification is allowed |
| created_at | string | Creation time (ISO 8601) |
| expire_at | string | Expiry time (ISO 8601) |
| triggered_at | string | Trigger time (ISO 8601) |
| updated_at | string | Last updated time (ISO 8601) |
