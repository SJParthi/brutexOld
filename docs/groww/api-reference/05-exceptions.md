# 05 — Exceptions · Groww Trading API (Python SDK)

> **Source:** https://groww.in/trade-api/docs/python-sdk/exceptions
> **Verified:** 12 June 2026
> **Module:** `growwapi.groww.exceptions`

The SDK provides custom exceptions to handle various error scenarios.

## GrowwBaseException
Base class for all exceptions in the Groww SDK. Captures the general error message. Use as a generic catch-all for errors that do not fall into more specific categories.
**Attributes:**
- `msg` (str): The error message associated with the exception.

## GrowwAPIException
Raised for client-related errors, such as invalid requests or authentication failures. Use to handle client-side issues such as invalid API keys or malformed requests.
**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPIAuthenticationException
Raised when authentication with the Groww API fails — issues with the API key or authentication process.
**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPIAuthorisationException
Raised when authorization with the Groww API fails — issues with the API key or access permissions.
**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPIBadRequestException
Raised when a bad request is made — issues with the request payload or parameters.
**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPINotFoundException
Raised when the requested resource is not found — a logical error in the code or an outdated reference.
**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPIRateLimitException
Raised when the rate limit for the Groww API is exceeded — too many requests in a short period; throttle the request rate.
**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPITimeoutException
Raised when a request to the Groww API times out — potential network issues or server overload.
**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

## GrowwFeedException
Raised for errors related to the Groww feed, such as connection issues or subscription failures.
**Attributes:**
- `msg` (str): The error message.

## GrowwFeedConnectionException
Raised when a connection to the Groww feed fails — establishing or maintaining a connection to the feed (crucial for live market data and updates).
**Attributes:**
- `msg` (str): The error message.

## GrowwFeedNotSubscribedException
Raised when trying to access data from a feed that has not been subscribed to. A subscription is required to receive data from the feed.
**Attributes:**
- `msg` (str): The error message.
- `topic` (str): The topic that must be subscribed to receive messages.

---

## Example handling pattern
```python
from growwapi.groww.exceptions import (
    GrowwAPIRateLimitException,
    GrowwAPITimeoutException,
    GrowwAPIException,
    GrowwBaseException,
)

try:
    candles = groww.get_historical_candles(...)
except GrowwAPIRateLimitException as e:
    # back off and retry
    print("Rate limited:", e.msg, e.code)
except GrowwAPITimeoutException as e:
    print("Timed out:", e.msg, e.code)
except GrowwAPIException as e:
    print("API error:", e.msg, e.code)
except GrowwBaseException as e:
    print("SDK error:", e.msg)
```

> Notes:
> - The `*Authentication`, `*Authorisation`, `*BadRequest`, `*NotFound`, `*RateLimit`, `*Timeout` exceptions sit under the `GrowwAPIException` family.
> - Catch `GrowwBaseException` as a final fallback. For HTTP-style errors, inspect the `code` attribute.
> - `GrowwFeedNotSubscribedException.topic` tells you which topic must be subscribed to receive messages.
