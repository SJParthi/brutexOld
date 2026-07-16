# Groww Trading API — Authentication

> Sources:
> - https://groww.in/trade-api/docs/curl (cURL Docs — "Introduction > Step 2: Authentication")
> - https://groww.in/trade-api/docs/python-sdk (Python SDK Docs — "Step 3: Authentication")
> Captured: 2026-07-15

There are two/three ways you can interact with GrowwAPI (the cURL docs list three approaches; the Python SDK docs list two — API Key+Secret and TOTP — since the daily access token is what both produce).

Key management pages:
- Trading APIs subscription & token management: https://groww.in/user/profile/trading-apis
- Groww Cloud API Keys Page (API key/secret and TOTP token generation, daily approval): https://groww.in/trade-api/api-keys

---

## 1st Approach: Access Token

**(Expires daily at 6:00 AM)**

To generate an API access token:

- Log in to your Groww account.
- Click on the profile section at the Right-top of your screen.
- Click on the setting icon in the menu.
- In the navigation list, select 'Trading APIs'
- Click on 'Generate API keys' and select 'Access Token'
- You can create, revoke and manage all your tokens from this page: https://groww.in/user/profile/trading-apis

Use the token directly as a Bearer token:

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/order/detail/{groww_order_id}?segment=CASH \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer YOUR_GENERATED_ACCESS_TOKEN' \
  -H 'X-API-VERSION: 1.0'
```

---

## 2nd Approach: API Key and Secret Flow

**(Uses API Key and Secret — Requires daily approval on Groww Cloud Api Keys Page)**

- Go to the Groww Cloud API Keys Page: https://groww.in/trade-api/api-keys
- Log in to your Groww account.
- Click on 'Generate API key'.
- Enter the name for the key and click Continue.
- Copy API Key and Secret. You can manage all your keys from the same page.

### Token-generation endpoint

`POST https://api.groww.in/v1/token/api/access`

```bash
curl -X POST "https://api.groww.in/v1/token/api/access" \
  -H "Authorization: Bearer <USER_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "key_type": "approval",
    "checksum": "<Checksum>",
    "timestamp": "1719830400"
  }'
```

This api requires a checksum and latest timestamp in epoch seconds in request body. See "How to Generate a Checksum" below.

#### Request Headers

| Header | Type | Description | Required |
| --------------- | ------ | ------------ | -------- |
| `Authorization` | String | User API Key | Yes |

#### Request Body

```json
{
  "key_type": "approval",
  "checksum": "abcdef1234567890",
  "timestamp": "1719830400"
}
```

| Parameter | Type | Description | Required |
| ----------- | ------ | -------------------------- | -------- |
| `key_type` | String | "approval" | Yes |
| `checksum` | String | HMAC or checksum signature | Yes |
| `timestamp` | String | Epoch seconds (10 digits) | Yes |

#### Response

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "tokenRefId": "ref-123",
  "sessionName": "my-session",
  "expiry": "2024-07-01T12:34:56",
  "isActive": true
}
```

| Parameter | Type | Description |
| ------------- | ------- | ----------------------------- |
| `token` | String | The generated access token |
| `tokenRefId` | String | Reference ID for the token |
| `sessionName` | String | Name of the session |
| `expiry` | String | Expiry date-time (ISO format) |
| `isActive` | Boolean | Token status |

### Python SDK usage (API Key + Secret)

Make sure you have the latest SDK version (`pip install --upgrade growwapi`).

```python
from growwapi import GrowwAPI
import pyotp

api_key = "YOUR_API_KEY"
secret = "YOUR_API_SECRET"

access_token = GrowwAPI.get_access_token(api_key=api_key, secret=secret)
# Use access_token to initiate GrowwAPI
groww = GrowwAPI(access_token)
```

---

## 3rd Approach: TOTP Flow

**(cURL docs: Uses API Key and Totp code — Requires daily approval on Groww Cloud Api keys page.**
**Python SDK docs: Uses TOTP token and TOTP QR code — No Expiry.)**

Setup (from Python SDK docs):

- Go to the Groww Cloud API Keys Page: https://groww.in/trade-api/api-keys
- Log in to your Groww account.
- Click on 'Generate TOTP token' which is under the dropdown to `Generate API Key` button.
- Enter the name for the key and click Continue.
- Copy the TOTP token and Secret or scan the QR via a third party authenticator app.
- You can manage all your keys from the same page.

### Token-generation endpoint (TOTP)

`POST https://api.groww.in/v1/token/api/access`

```bash
curl -X POST "https://api.groww.in/v1/token/api/access" \
  -H "Authorization: Bearer <USER_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "key_type": "totp",
    "totp": "<TOTP_CODE>"
  }'
```

#### Request Headers

| Header | Type | Required | Description |
| --------------- | ------ | -------- | ------------ |
| `Authorization` | String | Yes | User API Key |

#### Request Body

```json
{
  "key_type": "totp",
  "totp": "123456"
}
```

| Parameter | Type | Description | Required |
| ---------- | ------ | --------------------------------------------------------------------- | -------- |
| `key_type` | String | "totp" | Yes |
| `totp` | String | TOTP code generated by the user using a third-party authenticator app | Yes |

### Python SDK usage (TOTP)

Requires the `pyotp` library:

```bash
pip install pyotp
```

```python
from growwapi import GrowwAPI
import pyotp

api_key = "YOUR_TOTP_TOKEN"

# totp can be obtained using the authenticator app or can be generated like this
totp_gen = pyotp.TOTP('YOUR_TOTP_SECRET')
totp = totp_gen.now()

access_token = GrowwAPI.get_access_token(api_key=api_key, totp=totp)
# Use access_token to initiate GrowwAPI
groww = GrowwAPI(access_token)
```

---

## How to Generate a Checksum

Checksum should be a **SHA256 hash of api secret and latest timestamp in epoch seconds concatenated together** (`secret + timestamp`).

Parameters:

- **secret**: The api secret obtained from website
- **timestamp**: The latest timestamp value in epoch second. **Valid for 10 minutes.** Provide the same value in request.

### Python

```python
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

### Java

```java
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

### .NET

```csharp
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

### JavaScript

```javascript
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

---

> **Note**
>
> Use the correct type in the request body to select the authentication mode. "approval" for api key and secret, "totp" for api key and totp.
>
> All headers are mandatory.
