# Groww Trade API — Exceptions (SDK Error Classes)

> **Source:** https://groww.in/trade-api/docs/python-sdk/exceptions
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Exceptions

The SDK provides custom exceptions to handle various error scenarios.

Below are the custom exceptions and their business context. These exceptions are located in the `growwapi.groww.exceptions` module.

## [GrowwBaseException](#growwbaseexception)

This is the base class for all exceptions in the Groww SDK. It captures the general error message.

Expect this exception as a generic catch-all for errors that do not fall into more specific categories.

**Attributes:**

- `msg` (str): The error message associated with the exception.

## [GrowwAPIException](#growwapiexception)

This exception is raised for client-related errors, such as invalid requests or authentication failures.

Expect this exception to handle errors related to client-side issues, such as invalid API keys or malformed requests.

**Attributes:**

- `msg` (str): The error message.
- `code` (str): The error code.

### [GrowwAPIAuthenticationException](#growwapiauthenticationexception)

This exception is raised when authentication with the Groww API fails.

Expect this exception to handle scenarios where the SDK fails to authenticate with the Groww API, indicating issues with the API key or authentication process.

**Attributes:**

- `msg` (str): The error message.
- `code` (str): The error code.

### [GrowwAPIAuthorisationException](#growwapiauthorisationexception)

This exception is raised when authorization with the Groww API fails.

Expect this exception to handle scenarios where the SDK fails to authorize with the Groww API, indicating issues with the API key or access permissions.

**Attributes:**

- `msg` (str): The error message.
- `code` (str): The error code.

### [GrowwAPIBadRequestException](#growwapibadrequestexception)

This exception is raised when a bad request is made to the Groww API.

Expect this exception to handle scenarios where the SDK sends a malformed request to the API, indicating issues with the request payload or parameters.

**Attributes:**

- `msg` (str): The error message.
- `code` (str): The error code.

### [GrowwAPINotFoundException](#growwapinotfoundexception)

This exception is raised when the requested resource is not found on the Groww API.

Expect this exception to handle scenarios where the SDK requests a resource that does not exist, indicating a logical error in the code or an outdated reference.

**Attributes:**

- `msg` (str): The error message.
- `code` (str): The error code.

### [GrowwAPIRateLimitException](#growwapiratelimitexception)

This exception is raised when the rate limit for the Groww API is exceeded.

Expect this exception to handle scenarios where the SDK makes too many requests to the API within a short period, indicating a need to throttle the request rate.

**Attributes:**

- `msg` (str): The error message.
- `code` (str): The error code.

### [GrowwAPITimeoutException](#growwapitimeoutexception)

This exception is raised when a request to the Groww API times out.

Expect this exception to handle scenarios where the API request takes too long to respond, indicating potential network issues or server overload.

**Attributes:**

- `msg` (str): The error message.
- `code` (str): The error code.

## [GrowwFeedException](#growwfeedexception)

This exception is raised for errors related to the Groww feed.

Expect this exception to handle errors related to the feed, such as connection issues or subscription failures.

**Attributes:**

- `msg` (str): The error message.

## [GrowwFeedConnectionException](#growwfeedconnectionexception)

This exception is raised when a connection to the Groww feed fails.

Expect this exception to handle errors related to establishing or maintaining a connection to the Groww feed, which is crucial for receiving live market data and updates.

**Attributes:**

- `msg` (str): The error message.

## [GrowwFeedNotSubscribedException](#growwfeednotsubscribedexception)

This exception is raised when trying to access data from a feed that has not been subscribed to. A subscription is required to receive data from the feed.

Expect this exception to handle scenarios where the SDK attempts to retrieve data from a feed that has not been subscribed to, indicating a logical error in the code.

**Attributes:**

- `msg` (str): The error message.
- `topic` (str): The topic that must be subscribed to receive messages.

[Previous

Annexures](/trade-api/docs/python-sdk/annexures)[Next

Changelog](/trade-api/docs/python-sdk/changelog)
