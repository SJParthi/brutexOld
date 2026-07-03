# Groww Trade API — REST API: Portfolio

> **Source:** https://groww.in/trade-api/docs/curl/portfolio
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Portfolio

Get detailed information about your holdings and positions.

## [Get Holdings](#get-holdings)

`GET https://api.groww.in/v1/holdings/user`

This API can be used to retrieve the current stock holdings of the user stored in the user's DEMAT account

### [Request](#request)

```
# You can also use wget
curl -X GET https://api.groww.in/v1/holdings/user \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### [Response](#response)

```
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

#### [Response Schema](#response-schema)

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed |
| isin | string | The ISIN (International Securities Identification number) of the symbol |
| trading\_symbol | string | The trading symbol of the holding |
| quantity | integer | The net quantity of the holding |
| average\_price | decimal | The average price of the holding in Rupees |
| pledge\_quantity | decimal | The pledged quantity of the holding |
| demat\_locked\_quantity | decimal | The demat locked quantity of the holding |
| groww\_locked\_quantity | decimal | The Groww locked quantity of the holding |
| repledge\_quantity | decimal | The repledged quantity of the holding |
| t1\_quantity | decimal | The T1 quantity of the holding |
| demat\_free\_quantity | decimal | The demat free quantity of the holding |
| corporate\_action\_additional\_quantity | integer | The corporate action additional quantity of the holding |
| active\_demat\_transfer\_quantity | integer | The active demat transfer quantity of the holding |

## [Get Position for User](#get-position-for-user)

`GET https://api.groww.in/v1/positions/user`

This API can be used to get the positions of the user. Positions are the assets that the user holds in his/her account.

### [Request](#request-1)

```
# You can also use wget
curl -X GET https://api.groww.in/v1/positions/user \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

#### [Request schema](#request-schema)

| Name | Type | Description |
| --- | --- | --- |
| segment | string | [Segment](/trade-api/docs/curl/annexures#segment) of the instrument such as CASH, FNO, COMMODITY etc.  `CASH` `FNO` `COMMODITY` |

`*`required parameters

### [Response](#response-1)

```
{
  "status": "SUCCESS",
  "payload": {
    "positions": [
      {
        "trading_symbol": "RELIANCE",
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

#### [Response Schema](#response-schema-1)

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed |
| trading\_symbol | string | Trading symbol of the instrument |
| segment | string | [Segment](/trade-api/docs/curl/annexures#segment) of the instrument such as CASH, FNO, COMMODITY etc. |
| credit\_quantity | int | Quantity of credited instruments |
| credit\_price | int | Average price in rupees of credited instruments |
| debit\_quantity | int | Quantity of debited instruments |
| debit\_price | int | Average price in rupees of debited instruments |
| carry\_forward\_credit\_quantity | int | Quantity of carry forward credited instruments |
| carry\_forward\_credit\_price | int | Average price in rupees of carry forward credited instruments |
| carry\_forward\_debit\_quantity | int | Quantity of carry forward debited instruments |
| carry\_forward\_debit\_price | int | Average price in rupees of carry forward debited instruments |
| exchange | string | [Stock exchange](/trade-api/docs/curl/annexures#exchange) |
| symbol\_isin | string | ISIN (International Securities Identification number) of the symbol |
| quantity | int | Net quantity of instruments |
| product | string | [Product type](/trade-api/docs/curl/annexures#product) |
| net\_carry\_forward\_quantity | int | Net carry forward quantity of instruments |
| net\_price | int | Net average price in rupees of instruments |
| net\_carry\_forward\_price | int | Net average price in rupees of carry forward instruments |
| realised\_pnl | int | Realised profit and loss in rupees for the instrument |

## [Get Position for trading Symbol](#get-position-for-trading-symbol)

`GET https://api.groww.in/v1/positions/trading-symbol`

This API can be used to get the positions of the user for a particular instrument.

### [Request](#request-2)

```
# You can also use wget
curl -X GET https://api.groww.in/v1/positions/trading-symbol?trading_symbol=string&segment=CASH \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

#### [Request schema](#request-schema-1)

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed |
| trading\_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |
| segment | string | [Segment](/trade-api/docs/curl/annexures#segment) of the instrument such as CASH, FNO, COMMODITY etc.  `CASH` `FNO` `COMMODITY` |

`*`required parameters

### [Response](#response-2)

```
{
  "status": "SUCCESS",
  "payload": {
    "positions": [
      {
        "trading_symbol": "RELIANCE",
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

#### [Response Schema](#response-schema-2)

| Name | Type | Description |
| --- | --- | --- |
| trading\_symbol | string | Trading symbol of the instrument. |
| segment | string | [Segment](/trade-api/docs/curl/annexures#segment) of the instrument such as CASH, FNO, COMMODITY etc. |
| credit\_quantity | int | Quantity of credited instruments |
| credit\_price | int | Average price in rupees of credited instruments |
| debit\_quantity | int | Quantity of debited instruments |
| debit\_price | int | Average price in rupees of debited instruments |
| carry\_forward\_credit\_quantity | int | Quantity of carry forward credited instruments |
| carry\_forward\_credit\_price | int | Average price in rupees of carry forward credited instruments |
| carry\_forward\_debit\_quantity | int | Quantity of carry forward debited instruments |
| carry\_forward\_debit\_price | int | Average price in rupees of carry forward debited instruments |
| exchange | string | [Stock exchange](/trade-api/docs/curl/annexures#exchange) |
| symbol\_isin | string | ISIN (International Securities Identification number) of the symbol |
| quantity | int | Net quantity of instruments |
| product | string | [Product type](/trade-api/docs/curl/annexures#product) |
| net\_carry\_forward\_quantity | int | Net carry forward quantity of instruments |
| net\_price | int | Net average price in rupees of instruments |
| net\_carry\_forward\_price | int | Net average price in rupees of carry forward instruments |
| realised\_pnl | int | Realised profit and loss in rupees for the instrument |

[Previous

Smart Orders](/trade-api/docs/curl/smart-orders)[Next

Margin](/trade-api/docs/curl/margin)
