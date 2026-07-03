# Groww Trade API — REST API: Introduction (Auth flows, Checksum, Rate Limits, Error Codes)

> **Source:** https://groww.in/trade-api/docs/curl
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Introduction

Welcome to the Groww Trading API! Our APIs enable you to build and automate trading strategies with seamless access to real-time market data, order placement, portfolio management, and more. Whether you're an experienced algo trader or just starting with automation, Groww's API is designed to be simple, powerful, and developer-friendly.

This documentation focuses on using cURL to interact with the Groww Trading APIs. It provides step-by-step instructions, examples, and best practices for making API requests, handling responses, and integrating Groww's trading functionalities into your applications. Whether you're placing orders, fetching market data, or managing your portfolio, this guide will help you leverage cURL effectively for seamless API integration.

# [Getting Started](#getting-started)

## [Step 1: Prerequisites](#step-1-prerequisites)

Trading on Groww using Groww APIs requires:

- A Groww account.
- Basic knowledge of REST APIs.
- Having an active Trading API Subscription. You can purchase a subscription from this [page](https://groww.in/user/profile/trading-apis).

> **Important:** Groww Trading APIs currently support equity (CASH) and derivatives (FNO) trading only. Commodities trading (MCX segment) is not available at this time.

## [Step 2: Authentication](#step-2-authentication)

There are two ways you can interact with GrowwAPI:

### [1st Approach: Access Token](#1st-approach-access-token)

(Expires daily at 6:00 AM)

To generate an API access token:

- Log in to your Groww account.
- Click on the profile section at the Right-top of your screen.
- Click on the setting icon in the menu.
- In the navigation list, select ‘Trading APIs’
- Click on ‘Generate API keys’ and select ‘Access Token’
- You can create, revoke and manage all your tokens from this [page](https://groww.in/user/profile/trading-apis).

```
# You can also use wget
curl -X GET https://api.groww.in/v1/order/detail/{groww_order_id}?segment=CASH \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer YOUR_GENERATED_ACCESS_TOKEN' \
  -H 'X-API-VERSION: 1.0'
```

You can create, revoke and manage all your tokens from this [page](https://groww.in/user/profile/trading-apis).

### [2nd Approach: API Key and Secret Flow](#2nd-approach-api-key-and-secret-flow)

(Uses API Key and Secret — Requires daily approval on Groww Cloud Api Keys Page)

- Go to the [Groww Cloud API Keys Page](https://groww.in/trade-api/api-keys).
- Log in to your Groww account.
- Click on ‘Generate API key’.
- Enter the name for the key and click Continue.
- Copy API Key and Secret.
  You can manage all your keys from the same page

To generate access token using GrowwApi python sdk please check this [page](https://groww.in/trade-api/docs/python-sdk#step-1-prerequisites).
Else you can use the below curl command to generate access token.

```
curl -X POST "https://api.groww.in/v1/token/api/access" \
  -H "Authorization: Bearer <USER_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "key_type": "approval",
    "checksum": "<Checksum>",
    "timestamp": "1719830400"
  }'
```

This api requires a checksum and latest timestamp in epoch seconds in request body. Please check [How to generate checksum](#how-to-generate-a-checksum) to generate the checksum using your secret.

#### [Request Headers](#request-headers)

| Header | Type | Description | Required |
| --- | --- | --- | --- |
| `Authorization` | String | User API Key | Yes |

#### [Request Body](#request-body)

```
{
  "key_type": "approval",
  "checksum": "abcdef1234567890",
  "timestamp": "1719830400"
}
```

| Parameter | Type | Description | Required |
| --- | --- | --- | --- |
| `key_type` | String | "approval" | Yes |
| `checksum` | String | HMAC or checksum signature | Yes |
| `timestamp` | String | Epoch seconds (10 digits) | Yes |

#### [Response](#response)

```
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "tokenRefId": "ref-123",
  "sessionName": "my-session",
  "expiry": "2024-07-01T12:34:56",
  "isActive": true
}
```

| Parameter | Type | Description |
| --- | --- | --- |
| `token` | String | The generated access token |
| `tokenRefId` | String | Reference ID for the token |
| `sessionName` | String | Name of the session |
| `expiry` | String | Expiry date-time (ISO format) |
| `isActive` | Boolean | Token status |

### [3rd Approach: TOTP Flow](#3rd-approach-totp-flow)

(Uses API Key and Totp code — Requires daily approval on [Groww Cloud Api keys page](https://groww.in/trade-api/api-keys))
To generate token using Groww Python SDK, check out [this](https://groww.in/trade-api/docs/python-sdk#step-1-prerequisites) page.
Else use this curl command to generate access token.

```
curl -X POST "https://api.groww.in/v1/token/api/access" \
  -H "Authorization: Bearer <USER_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "key_type": "totp",
    "totp": "<TOTP_CODE>"
  }'
```

#### [Request Headers](#request-headers-1)

| Header | Type | Required | Description |
| --- | --- | --- | --- |
| `Authorization` | String | Yes | User API Key |

#### [Request Body](#request-body-1)

```
{
  "key_type": "totp",
  "totp": "123456"
}
```

| Parameter | Type | Description | Required |
| --- | --- | --- | --- |
| `key_type` | String | "totp" | Yes |
| `totp` | String | TOTP code generated by the user using a third-party authenticator app | Yes |

# [API Request and Response structure](#api-request-and-response-structure)

## [Headers](#headers)

All requests must have following headers. Providing the generated access token in the Authorization header .

| Header Name | Header Value |
| --- | --- |
| Authorization | Bearer `{ACCESS_TOKEN}` |
| Accept | application/json |
| X-API-VERSION | 1.0 |

## [Request structure](#request-structure)

**GET** Requests: Send the required parameters as query parameters in the request. For example,

```
# You can also use wget
curl -X GET https://api.groww.in/v1/order/detail/{groww_order_id}?segment=CASH \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer ACCESS_TOKEN' \
  -H 'X-API-VERSION: 1.0'
```


```
{
  "status": "SUCCESS",
  "payload": {
    "groww_order_id": "GMK39038RDT490CCVRO",
    "trading_symbol": "RELIANCE-EQ",
    "order_status": "OPEN",
    "remark": "Order placed successfully",
    "quantity": 100,
    "price": 2500,
    "trigger_price": 2450,
    "filled_quantity": 100,
    "remaining_quantity": 10,
    "average_fill_price": 2500,
    "deliverable_quantity": 10,
    "amo_status": "PENDING",
    "validity": "DAY",
    "exchange": "NSE",
    "order_type": "MARKET",
    "transaction_type": "BUY",
    "segment": "CASH",
    "product": "CNC",
    "created_at": "2023-10-01T10:15:30",
    "exchange_time": "2023-10-01T10:15:30",
    "trade_date": "2019-08-24T14:15:22Z",
    "order_reference_id": "Ab-654321234-1628190"
  }
}
```

**POST** Requests: Parameters are sent in the request body as JSON. For example,

```
    curl -X POST https://api.groww.in/v1/order/create \
      -H 'Content-Type: application/json' \
      -H 'Accept: application/json' \
      -H 'Authorization: Bearer {ACCESS_TOKEN}' \
      -H 'X-API-VERSION: 1.0' \
      -d '{
        "validity": "DAY",
        "exchange": "NSE",
        "transaction_type": "BUY",
        "order_type": "MARKET",
        "price": 0,
        "product": "CNC",
        "quantity": 1,
        "segment": "CASH",
        "trading_symbol": "IDEA"
    }'
```

Responses from the API are always JSON.

## [Successful Request (HTTP 200 OK)](#successful-request-http-200-ok)

When a request is successfully processed, the API returns a JSON object with a status field set to **SUCCESS**. The payload field contains the requested data.

```
{
    "status": "SUCCESS",
    "payload": {
        "symbolIsin": "INE002A01018",
        "productWisePositions": {}
    }
}
```

## [Failed Request (HTTP 40x or 50x)](#failed-request-http-40x-or-50x)

If a request fails, the API returns a JSON object with a status field set to **FAILURE**. The error field contains details about the failure.

```
{
    "status": "FAILURE",
    "error": {
        "code": "GA001",
        "message": "Invalid trading symbol.",
        "metadata": null
    }
}
```

## [Error Codes](#error-codes)

| Code | Message |
| --- | --- |
| GA000 | Internal error occurred |
| GA001 | Bad request |
| GA003 | Unable to serve request currently |
| GA004 | Requested entity does not exist |
| GA005 | User not authorised to perform this operation |
| GA006 | Cannot process this request |
| GA007 | Duplicate order reference id |

## [Rate Limits](#rate-limits)

The rate limits are applied at the type level, not on individual APIs. This means that all APIs grouped under a type (e.g., Orders, Live Data, Non Trading) share the same limit. If the limit for one API within a type is exhausted, all other APIs in that type will also be rate-limited until the limit window resets.

| Type | Requests | Limit (Per second) | Limit (Per minute) |
| --- | --- | --- | --- |
| Orders | Create, Modify and Cancel Order | 10 | 250 |
| Live Data | Market Quote, LTP, OHLC | 10 | 300 |
| Non Trading | Order Status, Order list, Trade list, Positions, Holdings, Margin | 20 | 500 |

#### [How to Generate a Checksum](#how-to-generate-a-checksum)

Checksum should be a SHA256 hash of api secret and and latest timestamp in epoch second concatenated together.

Following code snippets can be used to generate checksum, provided in multiple languages.

> #### [Python](#python)

```
import hashlib
import time
 
def generate_checksum(secret: str, timestamp :str) -> str:
    """
    Generates a SHA-256 checksum for the given data and salt.
    :param secret: The api secret value
    :return: Hexadecimal SHA-256 checksum
    """
    input_str = secret + timestamp
    sha256 = hashlib.sha256()
    sha256.update(input_str.encode('utf-8'))
    return sha256.hexdigest()  
 
timestamp = int(time.time()) # Timestamp in epoch seconds
secret = "<Your secret here>"
checksum = generate_checksum(secret, str(timestamp))
print(checksum)
```

> #### [Java](#java)

```
 
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.nio.charset.StandardCharsets;
import java.util.HexFormat;
import java.time.Instant;
 
public class ChecksumGenerator {
 
  private static final String SHA_256 = "SHA-256";
 
  public static void main(String[] args){
    System.out.println(generateChecksum("<your secret here>", getLatestTimestampinEpochSeconds()));
  }
 
  /**
   * Generates a SHA-256 checksum for the given input string.
   */
  public static String generateChecksum(String secret, String timestamp) {
    try {
      String input = secret + timestamp;
      MessageDigest digest = MessageDigest.getInstance(SHA_256);
      byte[] hash = digest.digest(input.getBytes(StandardCharsets.UTF_8));
      return HexFormat.of().formatHex(hash);
    } catch (NoSuchAlgorithmException e) {
      throw new RuntimeException("SHA-256 algorithm not found", e);
    }
  }
 
  /**
   * Return the latest time in epoch Seconds in String
   */
  public static String getLatestTimestampinEpochSeconds(){
    return String.format("%d", Instant.now().getEpochSecond());
  }
}
```

> ### [.NET](#net)

```
using System.Security.Cryptography;
using System.Text;
 
public class ChecksumGenerator
{
    public static void Main(string[] args)
    {
        Console.WriteLine(GenerateChecksum("<your secret here>", GetLatestTimestampInEpochSeconds()));
    }
 
    /// <summary>Generates a SHA-256 checksum for the given input string.</summary>
    public static string GenerateChecksum(string secret, string timestamp)
    {
        string input = secret + timestamp;
        byte[] inputBytes = Encoding.UTF8.GetBytes(input);
        byte[] hashBytes = SHA256.HashData(inputBytes);
        return Convert.ToHexString(hashBytes).ToLowerInvariant();
    }
 
    /// <summary>Returns the current UTC time in epoch seconds as a string.</summary>
    public static string GetLatestTimestampInEpochSeconds()
    {
        return DateTimeOffset.UtcNow.ToUnixTimeSeconds().ToString();
    }
}
```

> ### [JavaScript](#javascript)

```
const crypto = require('crypto');
 
// Generates a SHA-256 checksum for the given input string.
function generateChecksum(secret, timestamp) {
  try {
    const input = secret + timestamp;
    console.info(timestamp)
    const hash = crypto.createHash('sha256');
    hash.update(input);
    return hash.digest('hex');
  } catch (error) {
    console.error("Checksum generation failed:", error);
    throw new Error("Failed to generate SHA-256 checksum.");
  }
}
 
// Returns the current time in epoch seconds as a string.
function getLatestTimestampinEpochSeconds() {
  return Math.floor(Date.now() / 1000).toString();
}
 
// Main execution block
function main() {
  const secret = "<your secret here>";
  const timestamp = getLatestTimestampinEpochSeconds();
  const checksum = generateChecksum(secret, timestamp);
    console.log(checksum);
}
 
// Run the main function.
main();
```

##### [Parameters:](#parameters)

- secret: The api secret obtained from website
- timestamp: The latest timestamp value in epoch second. Valid for 10 minutes. Provide the same value in request.

---

> **Note**
>
> > Use the correct type in the request body to select the authentication mode. "approval" for api key and secret, "totp" for api key and totp
> >
> > All headers are mandatory.

---

[Next

Instruments](/trade-api/docs/curl/instruments)
