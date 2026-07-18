# Groww Trading API — User

> Sources:
> - https://groww.in/trade-api/docs/curl/user
> - https://groww.in/trade-api/docs/python-sdk/user
> Captured: 2026-07-15

Get user profile information.

## Get User Profile

`GET https://api.groww.in/v1/user/detail`

This API retrieves the user's profile information including their unique identifiers, trading capabilities across exchanges, enabled segments, and DDPI status.

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/user/detail \
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

user_profile_response = groww.get_user_profile()
print(user_profile_response)
```

### Response

```json
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

(Python SDK returns the payload dict directly.)

### Response Schema

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed (REST wrapper only) |
| vendor_user_id | string | Unique identifier of the user |
| ucc | string | Unique Client Code (UCC) of the user |
| nse_enabled | boolean | Whether trading is enabled on National Stock Exchange (NSE) for this user |
| bse_enabled | boolean | Whether trading is enabled on Bombay Stock Exchange (BSE) for this user |
| ddpi_enabled | boolean | DDPI (Demat Debit and Pledge Instruction) status. When enabled, allows the broker to debit securities from the user's Demat account for transactions like selling shares or pledging them for margin, without requiring TPIN and OTP authorization for each transaction |
| active_segments | array[string] | List of trading [segments](./13-annexures.md#segment) active for the user such as CASH, FNO, COMMODITY etc. |
