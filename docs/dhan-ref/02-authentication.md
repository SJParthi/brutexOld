# Dhan v2 — Authentication

> **Verified source:** DhanHQ v2 auth docs +
> `tickvault/docs/dhan-ref/02-authentication.md` (fetched 2026-05-31).

## Mechanisms

Dhan supports three auth paths:

1. **Access Token (individual)** — generated from Dhan Web
   (My Profile → Access DhanHQ APIs) **or** programmatically via TOTP.
2. **API Key & Secret (OAuth)** — 12-month validity, consent-based
   browser login.
3. **Partner Authentication** — three-step OAuth for platforms.

## HTTP headers

| Header | Value | Notes |
|---|---|---|
| `access-token` | JWT | **Primary** auth header on every Data-API call |
| `dhanClientId` | client id | required on some endpoints |
| `Content-Type` | `application/json` | POST body endpoints |
| `Accept` | `application/json` | |

## Token lifetime

| Method | Validity |
|---|---|
| Web-generated token | 24 hours (IST expiry) |
| TOTP-generated token | 24 hours, renewable |
| API Key & Secret | 12 months |
| OAuth access token | 24 hours |

> "One active token at a time — generating a new token invalidates the
> old one."

## The three token mechanisms (official, verbatim params)

**1. Manual access token (web.dhan.co):** Login → My Profile → "Access
DhanHQ APIs" → generate a 24-hour token (optional Postback URL). Needs a
human click.

**2. `generateAccessToken` (TOTP) — requires TOTP enabled:**

```
POST https://auth.dhan.co/app/generateAccessToken?dhanClientId={id}&pin={pin}&totp={code}
```

Query params: `dhanClientId`, `pin` (6-digit), `totp` (authenticator
code). Response: `{ dhanClientId, dhanClientName, dhanClientUcc,
givenPowerOfAttorney, accessToken, expiryTime }` (expiry = 24h). **This
endpoint IS the TOTP path — it requires the `totp` param; it is not a
non-TOTP alternative.**

**3. `RenewToken` — refresh an active token (+24h), NO TOTP:**

```
GET https://api.dhan.co/v2/RenewToken
  header: access-token: {JWT}
  header: dhanClientId: {id}
```

> **`RenewToken` is an HTTP GET — headers-only, NO request body**
> (DEC-DHAN-RENEWTOKEN-GET-001). Source: the official DhanHQ Python SDK
> (`dhan-oss/DhanHQ-py`, `src/dhanhq/auth.py`) calls it as
> `requests.get(url, headers=headers)` — a GET with the two headers and no
> body. (An earlier revision of this doc wrongly said `POST`; sending it as a
> POST with a JSON body makes Dhan reject it with `HTTP 400 DH-905` "Missing
> required fields, bad values for parameters".)

> "This only renews tokens which are **active**. If you try to renew an
> **expired** token, it will return an error." Also: renews only tokens
> **generated from Dhan Web**.

## DEFAULT auth path (operator decision 2026-05-31): manual token + RenewToken

`brutex/adapters/dhan/auth.py` implements: read the manual 24-h token +
client id from env (`BRUTEX_DHAN_ACCESS_TOKEN` / `BRUTEX_DHAN_CLIENT_ID`),
and **`renew_token()`** to extend it via `RenewToken` while active. TOTP
`generateAccessToken` stays a documented alternative for unattended runs.

## §11 AUTOMATED — honest envelope (Golden Rule #10)

- **`RenewToken` is NOT fully zero-touch:** it only extends an *active*,
  *web-generated* token; once expired it errors and a fresh token must be
  generated manually. So the default path is "automated while active,
  manual on expiry" — fine for operator-run research, but it does **not**
  meet §11 "ZERO clicks/day" on its own.
- **`generateAccessToken` (TOTP) IS the gap-proof unattended path** — it
  mints a fresh token from `pin`+`totp` anytime, even after expiry. It is
  the option for true §11 compliance.
- A refresh failure (expired token, `RenewToken` error) must **hard-stop +
  alert**, never silently serve 401s (mapped to `DhanAuthError`, terminal
  / non-retryable per `errors.is_retryable`).
- Secrets (token, client id, optional PIN/TOTP secret) live in env or a
  mode-0600 file; never logged, never committed.
