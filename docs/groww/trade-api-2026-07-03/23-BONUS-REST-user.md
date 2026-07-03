# Groww Trade API — REST API: User

> **Source:** https://groww.in/trade-api/docs/curl/user
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# User

Get user profile information

## [Get User Profile](#get-user-profile)

`GET https://api.groww.in/v1/user/detail`

This API retrieves the user's profile information including their unique identifiers, trading capabilities across exchanges, enabled segments, and DDPI status.

### [Request](#request)

```
# You can also use wget
curl -X GET https://api.groww.in/v1/user/detail \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### [Response](#response)

```
{
  "status": "SUCCESS",
  "payload": {
    "vendor_user_id": "d86890d1-c60d-4ebd-9730-4f451670",
    "ucc": "924189",
    "nse_enabled": true,
    "bse_enabled": true,
    "ddpi_enabled": false,
    "active_segments": [
      "CASH",
      "FNO",
      "COMMODITY"
    ]
  }
}
```

#### [Response Schema](#response-schema)

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed |
| vendor\_user\_id | string | Unique identifier of the user |
| ucc | string | Unique Client Code (UCC) of the user |
| nse\_enabled | boolean | Whether trading is enabled on National Stock Exchange (NSE) for this user |
| bse\_enabled | boolean | Whether trading is enabled on Bombay Stock Exchange (BSE) for this user |
| ddpi\_enabled | boolean | DDPI (Demat Debit and Pledge Instruction) status. When enabled, allows the broker to debit securities from the user's Demat account for transactions like selling shares or pledging them for margin, without requiring TPIN and OTP authorization for each transaction |
| active\_segments | array[string] | List of trading [Segment](/trade-api/docs/curl/annexures#segment) active for the user. |

[Previous

Backtesting](/trade-api/docs/curl/backtesting)[Next

Annexures](/trade-api/docs/curl/annexures)
