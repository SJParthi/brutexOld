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

## Programmatic (TOTP) flow — the AUTOMATED path

`POST https://auth.dhan.co/app/generateAccessToken` with
`dhanClientId`, PIN, and the current TOTP code returns a fresh 24-hour
access token.

## §11 AUTOMATED compliance (CLAUDE.md)

- The **TOTP path is ACCEPTED** — it requires **no daily manual
  Cloud-Console approval**, so "ZERO clicks per day" holds: a stored
  TOTP secret + PIN regenerates the 24-hour token unattended.
- The **manual web-regeneration path is REFUSED** for production
  automation (it needs a daily human click), exactly as the Groww
  "Approach 1 daily approval" was refused.
- Secrets (TOTP secret, PIN, client id) are stored **mode-0600** under
  `~/.brutex/dhan/`, never logged, never committed — mirrors the Groww
  credential handling that `brutex/config/credentials.py` already
  implements (to be re-pinned Groww→Dhan in the adapter-flip commit).

## Honest-envelope flags

- The 24-hour token expiry means the automation MUST refresh daily;
  the orchestrator's existing token-refresh hook (built for Groww
  TOTP) is reused. A refresh failure must hard-stop the pull, not
  silently serve stale 401s.
