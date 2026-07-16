# Introduction

> Source: https://dhanhq.co/docs/v2/ (DhanHQ Ver 2.0 / API Documentation)
> Captured: 2026-07-15 · Site generator: mkdocs-material (rendered page captured verbatim)

## Getting Started

DhanHQ API is a state-of-the-art platform for you to build trading and investment services & strategies.

It is a set of REST-like APIs that provide integration with our trading platform. Execute & modify orders in real time, manage portfolio, access live market data and more, with lightning fast API collection.

We offer resource-based URLs that accept JSON or form-encoded requests. The response is returned as JSON-encoded responses by using Standard HTTP response codes, verbs, and authentication.

- **Developer Kit**: https://api.dhan.co/v2/ (Swagger/OpenAPI explorer)
- **DhanHQ Python Client**: https://pypi.org/project/dhanhq/

## Structure

**REST**

All GET and DELETE request parameters go as query parameters, and POST and PUT parameters as form-encoded.
User has to input an access token in the header for every request.

```
curl --request POST \
--url https://api.dhan.co/v2/ \
--header 'Content-Type: application/json' \
--header 'access-token: JWT' \
--data '{Request JSON}'
```

**Python**

Install Python Package directly using following command in command line.

```
pip install dhanhq
```

This installs entire DhanHQ Python Client along with the required packages. Now, you can start using DhanHQ Client with your Python script.

You can now import 'dhanhq' module and connect to your Dhan account.

```
from dhanhq import dhanhq

dhan = dhanhq("client_id","access_token")
```

## Errors

Error responses come with the error code and message generated internally by the system. The sample structure of error response is shown below.

```
{
    "errorType": "",
    "errorCode": "",
    "errorMessage": ""
}
```

You can find detailed error code and message under [Annexure](./18-annexure.md#trading-api-error) (site: https://dhanhq.co/docs/v2/annexure/#trading-api-error).

## Rate Limit

| Rate Limit | Order APIs | Data APIs | Quote APIs | Non Trading APIs |
| ---------- | ---------- | --------- | ---------- | ---------------- |
| per second | 10         | 5         | 1          | 20               |
| per minute | 250        | -         | Unlimited  | Unlimited        |
| per hour   | 1000       | -         | Unlimited  | Unlimited        |
| per day    | 7000       | 100000    | Unlimited  | Unlimited        |

Order Modifications are capped at 25 modifications/order

---
Copyright © 2024 Moneylicious Securities Private Limited
