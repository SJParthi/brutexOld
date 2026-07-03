# Groww Trade API — User Profile

> **Source:** https://groww.in/trade-api/docs/python-sdk/user
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# User

This guide describes how to retrieve user profile information and account details using the SDK.

## [Get User Profile](#get-user-profile)

Retrieve detailed information about the authenticated user's trading account, including unique identifiers, exchange access permissions, enabled trading segments, and DDPI authorization status.

### [Python SDK Usage](#python-sdk-usage)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
user_profile_response = groww.get_user_profile()
print(user_profile_response)
```

### [Response](#response)

```
{
  "vendor_user_id": "d86890d1-c60d-4ebd-9730-4feee5451670",
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
```

#### [Response Schema](#response-schema)

| Name | Type | Description |
| --- | --- | --- |
| vendor\_user\_id | string | Unique identifier of the user |
| ucc | string | Unique Client Code (UCC) of the user |
| nse\_enabled | boolean | Whether trading is enabled on National Stock Exchange (NSE) for this user |
| bse\_enabled | boolean | Whether trading is enabled on Bombay Stock Exchange (BSE) for this user |
| ddpi\_enabled | boolean | DDPI (Demat Debit and Pledge Instruction) status. When enabled, allows the broker to debit securities from the user's Demat account for transactions like selling shares or pledging them for margin, without requiring TPIN and OTP authorization for each transaction |
| active\_segments | array[string] | List of trading [segments](/trade-api/docs/python-sdk/annexures#segment) active for the user such as CASH, FNO, COMMODITY etc. |

[Previous

Feed](/trade-api/docs/python-sdk/feed)[Next

Annexures](/trade-api/docs/python-sdk/annexures)
