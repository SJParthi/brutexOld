# 02 — Authentication (token mint, bearer, WS query auth, sandbox, trial)

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md).

## 1. Claims table

| Claim | Value | Source (access 2026-07-15) | Confidence |
|---|---|---|---|
| Credentials model | Single `login_id` (username) + `password` per subscription; the SAME credentials drive WS, history REST, analytics, corporate, and the sandbox | truedata-nodejs 1.2.0 + truedata 7.0.1 source (pkg-source); PyPI/npm READMEs (direct-fetch); KB via search-relay | high |
| Token endpoint | `POST https://auth.truedata.in/token` | Node `utils.js` + Python `Historical_REST.py` + `TD_analytics.py` (pkg-source) | high |
| Token request | body `username=<login_id>&password=<password>&grant_type=password`, header `Content-Type: application/x-www-form-urlencoded` (OAuth2 password-grant style) | `Historical_REST.py get_new_token()` + `historical.js auth()` (pkg-source) | high |
| Token response | JSON with `access_token`, `expires_in` (seconds); on failure `error_description` | `Historical_REST.py hist_login()` (pkg-source) | high |
| Token TTL | ~3600 s: Node hardcodes `expires_in = 3600 - 20` ("new token 20 seconds before current token expires"); Python trusts the server's `expires_in` minus a 15 s buffer | pkg-source | high (client-side evidence; server value carried in the response) |
| Refresh tokens | NOT seen — SDKs simply re-mint with the password; whether the response carries more fields than access_token/expires_in/(token_type) is UNVERIFIED | pkg-source (negative) | UNVERIFIED (extra fields) |
| Using the token | Every history/analytics/corporate REST call sends `Authorization: Bearer <access_token>` | `historical.js bearer()`; `TD_analytics.py` headers (pkg-source) | high |
| Re-auth trigger | Server replies `Message` starting `"Authorization has been denied for this request"` → SDK re-authenticates once and replays the request | `historical.js axioCall()` (pkg-source) | high |
| WS auth | Credentials in the WebSocket URL query string: `wss://push.truedata.in:<port>?user=<u>&password=<p>` — no token on WS | `feed.js connect()` + Python `TD_live.connect()` (pkg-source) | high |
| api.truedata.in auth | `user` + `password` as PLAIN QUERY PARAMS (not bearer), e.g. `https://api.truedata.in/getAllSymbols?segment=all&user=<u>&password=<p>&search=<sym>` | `feed.js:513`, `historical.js:201`, `optionChain.js` (pkg-source) | high |
| Environments | Production + Sandbox; the sandbox page `https://wstest.truedata.in` uses the same API credentials; "trials and access to real-time data during development provided on the sandbox environment" | PyPI READMEs (direct-fetch) + KB getting-started via search-relay | high (existence) / medium (wording) |
| Concurrent logins | WebSocket: "You can connect only from 1 place at a time (1 login instance)" | KB "Request/Call Limit…" via search-relay | medium |
| Legacy historical-WS login | The old historical-over-WebSocket service was deprecated — "available only up to 30 Apr 2021"; REST replaced it (truedata-ws v3.0.1 changelog: "Historical WebSocket deprecated; REST API now used") | Scribd doc summary via search-relay + PyPI truedata-ws changelog (direct-fetch) | medium/high |

## 2. Verbatim — token mint (vendor Python SDK 7.0.1, `truedata/history/Historical_REST.py`)

```python
def get_new_token(self ):
    url_auth = "https://auth.truedata.in/token"
    payload = f"username={self.login_id}&password={self.password}&grant_type=password"
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    token_json = requests.request("POST", url_auth, headers=headers, data=payload).json()
    return token_json

def hist_login(self):
    token_json = self.get_new_token()
    try:
        if token_json['access_token']:
            self.access_token = token_json['access_token']
            self.access_token_expiry_time = dt.now() + timedelta(seconds=token_json['expires_in'] - 15)  # 15 seconds is random buffer time
```

## 3. Verbatim — Node SDK auth + re-auth (`truedata-nodejs@1.2.0`)

```js
// utils.js
module.exports.expires_in = 3600 - 20; // - 20 is to make sure user gets new token 20 seconds before current token expires
module.exports.login = 'https://auth.truedata.in/token';

// historical.js
const params = new URLSearchParams();
params.append('username', user);
params.append('password', pwd);
params.append('grant_type', grant_type);   // grant_type = 'password'
const res = await axios.post(url, params);
time = Date.now() + res.data.expires_in * 1000;
token = { access_token: res.data.access_token, time };
...
function bearer() { return { headers: { Authorization: `Bearer ${token.access_token}` } }; }
```

Re-auth branch: if
`err.response.data.Message.startsWith('Authorization has been denied for this request')`
and the token is not yet locally expired → force re-auth and replay.

## 4. The three auth patterns, side by side (adapter-critical)

| Surface | Pattern | Credential exposure |
|---|---|---|
| history / analytics / greeks / corporate REST | Bearer token (minted via password grant, ~1 h) | password only at mint time (HTTPS body) |
| live / replay / corporate WS | `?user=…&password=…` in the connection URL | password in the URL every connect |
| api.truedata.in utility calls | `user=…&password=…` plain query params | password in the URL every call |

**BruteX/TickVault caution:** two of three patterns put the password in
URLs (log/proxy exposure surface). Any adapter must treat the pair as a
secret (AWS-SSM style per the house DEC-AWS-ONLY-CREDS-001 posture),
scrub URLs from logs, and re-mint tokens with the SDKs' early-buffer
discipline. Unattended-auth compatibility: the password grant is fully
scriptable — no daily interactive step was evidenced (contrast the
Zerodha finding in `docs/research/ZERODHA_HISTORICAL_FEASIBILITY.md`) —
but plan/ToS terms around automation were NOT retrieved (UNVERIFIED).

## 5. Trial & onboarding flow (KB "Getting Started", search-relay, medium)

Sign up on truedata.in/products/marketdataapi → vendor asks you to study
the docs first → up to **10 days free trial** with sandbox + real-time
data during development → pricing shared after a requirements review
(exchanges, symbols, data type). See [07-limits-plans.md](./07-limits-plans.md).

## 6. Recorded oddity

`Historical_REST.py:17` comment: `# NO PORT, broker token needed from now
on` — suggests the history REST once took a port and now uses token auth
("broker token" = the bearer). Recorded verbatim; interpretation low
confidence.
