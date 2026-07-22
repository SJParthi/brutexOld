# Groww Trading API — Error Codes & Python SDK Exceptions

> Sources:
> - https://groww.in/trade-api/docs/curl (Introduction > Error Codes / Failed Request)
> - https://groww.in/trade-api/docs/python-sdk/exceptions
> Captured: 2026-07-15

## REST error format

If a request fails (HTTP 40x or 50x), the API returns a JSON object with a status field set to **FAILURE**. The error field contains details about the failure.

```json
{
    "status": "FAILURE",
    "error": {
        "code": "GA001",
        "message": "Invalid trading symbol.",
        "metadata": null
    }
}
```

## Error Codes

| Code | Message |
| ----- | --------------------------------------------- |
| GA000 | Internal error occurred |
| GA001 | Bad request |
| GA003 | Unable to serve request currently |
| GA004 | Requested entity does not exist |
| GA005 | User not authorised to perform this operation |
| GA006 | Cannot process this request |
| GA007 | Duplicate order reference id |

---

# Python SDK Exceptions

The SDK provides custom exceptions to handle various error scenarios. These exceptions are located in the **`growwapi.groww.exceptions`** module.

Class hierarchy:

```text
GrowwBaseException
├── GrowwAPIException
│   ├── GrowwAPIAuthenticationException
│   ├── GrowwAPIAuthorisationException
│   ├── GrowwAPIBadRequestException
│   ├── GrowwAPINotFoundException
│   ├── GrowwAPIRateLimitException
│   └── GrowwAPITimeoutException
├── GrowwFeedException
├── GrowwFeedConnectionException
└── GrowwFeedNotSubscribedException
```

(Nesting of the API sub-exceptions under `GrowwAPIException` follows the docs page structure; feed exceptions are listed as separate top-level sections.)

## GrowwBaseException

This is the base class for all exceptions in the Groww SDK. It captures the general error message.

Expect this exception as a generic catch-all for errors that do not fall into more specific categories.

**Attributes:**
- `msg` (str): The error message associated with the exception.

## GrowwAPIException

This exception is raised for client-related errors, such as invalid requests or authentication failures.

Expect this exception to handle errors related to client-side issues, such as invalid API keys or malformed requests.

**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPIAuthenticationException

This exception is raised when authentication with the Groww API fails.

Expect this exception to handle scenarios where the SDK fails to authenticate with the Groww API, indicating issues with the API key or authentication process.

**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPIAuthorisationException

This exception is raised when authorization with the Groww API fails.

Expect this exception to handle scenarios where the SDK fails to authorize with the Groww API, indicating issues with the API key or access permissions.

**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPIBadRequestException

This exception is raised when a bad request is made to the Groww API.

Expect this exception to handle scenarios where the SDK sends a malformed request to the API, indicating issues with the request payload or parameters.

**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPINotFoundException

This exception is raised when the requested resource is not found on the Groww API.

Expect this exception to handle scenarios where the SDK requests a resource that does not exist, indicating a logical error in the code or an outdated reference.

**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPIRateLimitException

This exception is raised when the rate limit for the Groww API is exceeded.

Expect this exception to handle scenarios where the SDK makes too many requests to the API within a short period, indicating a need to throttle the request rate.

**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

### GrowwAPITimeoutException

This exception is raised when a request to the Groww API times out.

Expect this exception to handle scenarios where the API request takes too long to respond, indicating potential network issues or server overload.

**Attributes:**
- `msg` (str): The error message.
- `code` (str): The error code.

## GrowwFeedException

This exception is raised for errors related to the Groww feed.

Expect this exception to handle errors related to the feed, such as connection issues or subscription failures.

**Attributes:**
- `msg` (str): The error message.

## GrowwFeedConnectionException

This exception is raised when a connection to the Groww feed fails.

Expect this exception to handle errors related to establishing or maintaining a connection to the Groww feed, which is crucial for receiving live market data and updates.

**Attributes:**
- `msg` (str): The error message.

## GrowwFeedNotSubscribedException

This exception is raised when trying to access data from a feed that has not been subscribed to. A subscription is required to receive data from the feed.

Expect this exception to handle scenarios where the SDK attempts to retrieve data from a feed that has not been subscribed to, indicating a logical error in the code.

**Attributes:**
- `msg` (str): The error message.
- `topic` (str): The topic that must be subscribed to receive messages.
