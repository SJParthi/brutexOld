# Feed-probe runbook — DhanHQ v2 + Zerodha Kite Connect v3

A tiny **read-only** Phase-0 probe the operator runs **once, locally**, before any
DhanHQ or Kite historical-data adapter is built. The build sandbox cannot reach
any vendor host (all `*.dhan.co` / `kite.trade` egress is blocked), so these facts
must be pinned from a machine that _can_ reach them:

1. **(Dhan a)** the real 1-minute intraday **floor** per index (NIFTY 50 /
   BANKNIFTY / SENSEX / INDIA VIX) — do 2020 / 2021 / 2022 / 2023 windows return
   candles? Dhan's docs claim intraday is a rolling **"last 5 years"**, i.e. it
   may _not_ reach BruteX's `2020-01-01` floor. This settles it empirically.
2. **(Dhan b)** does INDIA VIX serve intraday candles **at all** (`IDX_I` /
   securityId 21) on a recent date?
3. **(Dhan c)** does the headless **PIN + TOTP** token mint work for this account
   (preconditions: is 2FA/TOTP enabled? is the Data API subscribed?)?
4. **(Dhan e)** does **SENSEX on `IDX_I`** (the BSE-index _spot_ row, securityId 51)
   serve 1-minute candles — settled by the recent window **plus a mid-2022 (June)
   anchor** added to the backward floor march, so it is checked off the January
   grid too.
5. **(Dhan f)** does the expired-options **rolling series**
   (`POST /charts/rollingoption`) return candles that carry **VOLUME** (+ oi / iv /
   spot) for **NSE index options** (NIFTY, `NSE_FNO`) **and BSE index options**
   (SENSEX, `BSE_FNO`)? This is the **make-or-break capability gate** for a later
   options phase — it settles rollingoption servability, options-carry-OHLCV-volume,
   and `BSE_FNO` servability in one shot.
6. **(Dhan g)** are the **three hardcoded ENGINE index securityIds correct** —
   NIFTY 50 = 13, BANKNIFTY = 25, SENSEX = 51? A wrong securityId silently pulls
   the **wrong instrument**, so the probe reads the id the public scrip-master CSV
   actually lists for each index and emits a loud **`CONFIRMED` / `MISMATCH` /
   `NOT-FOUND`** verdict (or `UNCONFIRMED-CSV-UNREACHABLE` if the CSV host is
   egress-blocked here too). This needs **no credentials** (the CSV is public) and
   runs on **every** Dhan run, so even a minimal Dhan-only run confirms the ids.
7. **(Kite d)** does **SENSEX** (BSE index, token 265) return historical candles
   today (a Jan-2024 forum thread reported it did **not**)? Plus INDIA VIX
   (264969) and BANKNIFTY (260105) minute depth at 2015, and a NIFTY (256265)
   recent control that auth works.

The tool is one stdlib-only script — `probe_feeds.py` — plus this runbook. No
`pip install`, no `brutex` import.

> **No secret is ever printed, logged, or written.** Credentials come only from
> environment variables; every emitted line (stdout, stderr, and the evidence
> file) is scrubbed of every registered secret. Tokens/keys/PINs never appear —
> not even in error bodies or URLs. The evidence file is safe to commit.

---

## 1. Prerequisites

### Both vendors

* Python 3.11+ (standard library only — nothing to install).
* Outbound HTTPS to the vendor hosts from the machine you run this on.

### DhanHQ (skip if you only want the Kite probe)

* An **active Dhan account** with the **Data API subscription** — ₹499 + taxes /
  month, auto-renewing every 30 days, **free if ≥ 25 trades were executed in the
  last 30 days**. Without it, historical calls return `403 Forbidden` (the probe
  reports this as `FORBIDDEN (403 …)`, it does not crash).
* Either a **current access token** (24-hour lifetime; resets daily), _or_ the
  **PIN + TOTP** credentials so the probe can mint one headlessly (goal c). For
  the mint the account must have **TOTP-based 2FA enabled** and the PIN set.
* Static-IP whitelisting is **not** required for read/data APIs (only for order
  placement), so the data probe needs no IP setup.

Environment variables (set the ones you have):

| Variable | Meaning |
| --- | --- |
| `DHAN_CLIENT_ID` | Your numeric Dhan client id. |
| `DHAN_ACCESS_TOKEN` | A current access token. **If set, the mint is skipped.** |
| `DHAN_PIN` | 6-digit login PIN (only needed to mint). |
| `DHAN_TOTP_SECRET` | Base32 TOTP shared secret (only needed to mint). |

If `DHAN_ACCESS_TOKEN` is absent but `DHAN_CLIENT_ID` + `DHAN_PIN` +
`DHAN_TOTP_SECRET` are all present, the probe attempts **one** PIN+TOTP mint and
reports only its _shape_ (never the token). With neither a token nor complete
mint creds, the Dhan **intraday** probes are marked `SKIPPED (no token)` — but
the public scrip-master securityId **resolution and ID-confirm (goal g) still
run**, so a Dhan run with **no credentials at all** still verifies the three
hardcoded engine securityIds against the public CSV.

### Zerodha Kite (skip if you only want the Dhan probe)

* A **Kite Connect** subscription (₹500 / month per API key). Historical data is
  bundled with the base subscription since Feb-2025.
* A **fresh daily access token.** Kite has **no** official headless mint for
  individuals — you log in through the browser once per day (the token is flushed
  each morning, roughly 05:00–08:00 IST). The probe does **not** mint it; you
  supply it. Generate it via your usual Kite login flow just before running.

Environment variables:

| Variable | Meaning |
| --- | --- |
| `KITE_API_KEY` | Your Kite Connect API key. |
| `KITE_ACCESS_TOKEN` | Today's freshly-minted access token. |

If either is missing, the Kite probes are marked `SKIPPED (no creds)` and the run
continues.

---

## 2. Run commands

Dry-run first — prints the exact call plan, reads **no** env vars, makes **no**
network request:

```bash
python3 docs/runbooks/feed-probe/probe_feeds.py --dry-run
```

Run both vendors (default), writing a committable JSON transcript:

```bash
export DHAN_CLIENT_ID=... DHAN_PIN=... DHAN_TOTP_SECRET=...   # or DHAN_ACCESS_TOKEN=...
export KITE_API_KEY=... KITE_ACCESS_TOKEN=...
python3 docs/runbooks/feed-probe/probe_feeds.py \
    --evidence-out ./feed-probe-evidence.json
```

**Dhan-only run (the common case — only Dhan creds set):** this runs _every_ Dhan
probe (scrip-master id resolution, the intraday floor march incl. SENSEX/BSE and
the June-2022 anchor, INDIA VIX, and the `/charts/rollingoption` options-capability
probe) and skips Kite automatically. For the intraday **and** rollingoption reads
you only need `DHAN_CLIENT_ID` + `DHAN_ACCESS_TOKEN` — **read-only, no PIN/TOTP**
(PIN/TOTP are only for the optional headless token _mint_):

```bash
export DHAN_CLIENT_ID=...        # numeric client id (sent as the client-id header)
export DHAN_ACCESS_TOKEN=...     # current 24h data-API access token
python3 docs/runbooks/feed-probe/probe_feeds.py --dhan \
    --evidence-out ./dhan.json
```

**No-creds ID-confirm (goal g only):** the scrip-master ID-confirm needs no
credentials, so even with zero env vars set a Dhan run confirms the three
hardcoded engine securityIds against the public CSV (the intraday/options/mint
probes report `SKIPPED (no token)`):

```bash
python3 docs/runbooks/feed-probe/probe_feeds.py --dhan --evidence-out ./ids.json
# look for the three `scrip-confirm: …` rows → CONFIRMED / MISMATCH / NOT-FOUND
```

Kite only, markdown transcript:

```bash
python3 docs/runbooks/feed-probe/probe_feeds.py --kite --evidence-out ./kite.md
```

Options:

* `--all` — run both (also the default when neither `--dhan` nor `--kite` given).
* `--evidence-out PATH` — output path; `.md` → markdown, anything else → JSON.
* `--no-rollingoption` — skip the Dhan expired-options `/charts/rollingoption`
  probes (they run **by default** as part of a Dhan run).
* `--no-scrip-confirm` — skip the Dhan scrip-master **ID-confirm** step (goal g;
  it needs no creds, adds no network call, and runs **by default**).
* `--call-budget N` — hard ceiling on total network calls (default 60; the full
  plan issues < 35 — Dhan alone ≈ 27; the ID-confirm reuses the one scrip-master
  GET and adds nothing to the budget).

Pacing is automatic: ≤ 5 req/s to Dhan, ≤ 3 req/s to Kite, one polite pause after
a `429`, and after two `401/403`s for a vendor the rest of its probes short-circuit
(no spam-retry). Provide credentials via env, never on the command line (a shell
history is not a secret store).

---

## 3. What each probe answers

**Dhan** (`POST /charts/intraday`, segment `IDX_I`, instrument `INDEX`,
interval `1`):

* `securityId` resolution — read live from the public scrip master
  (`api-scrip-master.csv`). If the CSV is unreachable, the probe falls back to the
  documented ids (NIFTY 50 = 13 _verified_, BANKNIFTY = 25, SENSEX = 51, INDIA VIX
  = 21) and marks them **`UNVERIFIED fallback`** — check that line.
* Per index, a `recent` 1-week window (servability — **goal b** for VIX) then
  backward windows at **2023-01 → 2020-01** (the **floor** — **goal a**), now with
  an added **2022-06** (June) anchor. The oldest window that returns `CANDLES (...)`
  is that index's intraday floor.
* **SENSEX on `IDX_I`** (**goal e**) — the BSE-index _spot_ row is probed at
  `recent` **and** `2022-06`, so a positive `CANDLES` on the `SENSEX @ recent` /
  `SENSEX @ 2022-06` rows confirms Dhan serves **BSE index 1-minute** off the
  January grid too.
* `pin_totp_mint` — the headless mint attempt (**goal c**): `MINT_OK`,
  `MINT_DENIED (401/403 …)` (bad PIN/TOTP, or 2FA/entitlement not set up), or
  `MINT_FAILED`. Only the response _shape_ is recorded.

**Dhan scrip-master ID-confirm** (public `api-scrip-master.csv`, **goal g** — the
honesty gate; runs by default, needs **no creds**, `--no-scrip-confirm` to skip):

* Three `scrip-confirm: {NIFTY 50 | BANKNIFTY | SENSEX}` rows compare the
  **hardcoded** engine securityId (13 / 25 / 51) against the id the CSV actually
  lists for that index. A wrong id would silently pull the **wrong instrument**, so
  a `MISMATCH` or `NOT-FOUND` is a **loud** verdict to act on before any adapter
  trusts these ids.
* **CSV columns relied on** (Dhan _compact_ `api-scrip-master.csv`; verified from
  the DhanHQ v2 instruments docs + the `dhan-oss/DhanHQ-py` SDK). Matching is
  **column-tolerant**, never positional — a header rename/re-order cannot silently
  break it — but the parse keys on:
  * **`SEM_SMST_SECURITY_ID`** — the securityId (located by a `SECURITY`+`ID`
    header-substring match).
  * **`SEM_INSTRUMENT_NAME`** = `INDEX` — the index-row marker (matched by cell
    content, not column position).
  * **`SEM_TRADING_SYMBOL`** / **`SEM_CUSTOM_SYMBOL`** — the human name matched by
    alias (`NIFTY 50`, `NIFTY BANK` → BANKNIFTY, `SENSEX`).
  * `SEM_EXM_EXCH_ID` (NSE/BSE) and `SEM_SEGMENT` (index segment; the API
    `exchangeSegment` is `IDX_I`) are informational.

  The evidence note records the security-id header name the run actually matched,
  so **the operator's live run confirms this column mapping** — if Dhan's live
  header differs, the note shows the real column (or `documented column
  SEM_SMST_SECURITY_ID (header not resolved this run)` when the CSV was
  unreachable).

**Dhan expired-options** (`POST /charts/rollingoption`, **goal f** — the
make-or-break options-capability gate; runs by default, `--no-rollingoption` to
skip):

* Body is copied **field-for-field** from the official SDK
  (`dhan-oss/DhanHQ-py` → `_historical_data.expired_options_data`):
  `expiryFlag=WEEK`, `expiryCode=0`, `strike=ATM`, `drvOptionType=CALL`,
  `interval=1`, and `requiredData=[open, high, low, close, volume, oi, iv, spot]`
  over a ~30-day recent-past window. The SDK also injects `dhanClientId` into the
  POST body on every call — the probe replicates that (the client id is a secret:
  registered, injected into the sent body/headers, and never written to the
  transcript).
* Two cases, run together, settle three things at once:
  * `rollingoption: NIFTY ATM CALL WEEK` — `exchangeSegment=NSE_FNO`,
    `instrument=OPTIDX`, underlying = the resolved **NIFTY** index securityId.
  * `rollingoption: SENSEX ATM CALL WEEK` — `exchangeSegment=BSE_FNO`,
    `instrument=OPTIDX`, underlying = the resolved **SENSEX** index securityId —
    this row also settles **`BSE_FNO` servability**.
* Each row reports HTTP status, whether candles came back (`CANDLES (N)` /
  `EMPTY` / `HTTP_200_UNRECOGNIZED_SHAPE`), and — appended to the verdict when
  candles are served — a **`· fields[…]`** tag listing which of
  `volume / oi / iv / spot` the response actually carried (that is the volume
  question). The probe's `notes` (in the JSON evidence) additionally record the
  detected fields and any `expiryCode->echo` (the `expiryCode 0` → actual-expiry
  date the response echoes). If Dhan's live response shape differs from the SDK's,
  a served body shows as `HTTP_200_UNRECOGNIZED_SHAPE` with the redacted body in
  the note — the operator's run confirms the exact shape.

**Kite** (`GET /instruments/historical/{token}/minute`):

* `NIFTY 50 256265 @ recent` — control: proves the api-key + access token work.
* `SENSEX 265 @ recent` — **goal d, the risk row.** `CANDLES` = BSE index history
  works; `EMPTY` / `HTTP_*` = the Jan-2024 "no SENSEX candles" report still holds.
* `INDIA VIX 264969` @ recent and @ 2015-01 — servability + minute depth.
* `BANKNIFTY 260105 @ 2015-01` — minute depth spot check.

---

## 4. Reading the transcript

Each probe row carries: the endpoint + a **redacted** request summary, the HTTP
status, the candle **count** and **first/last timestamp** (never the candle data
itself), and a one-word **verdict**:

| Verdict | Meaning |
| --- | --- |
| `CANDLES (N)` | Data served — N candles, with first/last ts. |
| `CANDLES (N) · fields[…]` | (rollingoption) N candles **plus** which of `volume / oi / iv / spot` the body carried — the options-capability answer. |
| `CONFIRMED (csv id X == hardcoded X)` | (scrip-confirm) the CSV lists exactly the hardcoded engine securityId — the id is correct. |
| `MISMATCH (csv id Y != hardcoded X)` | (scrip-confirm) **loud** — the CSV lists a **different** id; the hardcoded constant is wrong and must be fixed before any pull trusts it. |
| `NOT-FOUND (no IDX_I/INDEX row matched …)` | (scrip-confirm) **loud** — the index row was not located in the CSV; the hardcoded id stays **unverified** (check the alias/segment or the CSV shape). |
| `UNCONFIRMED-CSV-UNREACHABLE` | (scrip-confirm) the public CSV could not be fetched (host egress-blocked); **non-fatal** — the intraday probes still settle servability, the ids are just not confirmed this run. |
| `EMPTY (200, zero candles)` | Endpoint OK but genuinely no data for that window/instrument. |
| `HTTP_200_UNRECOGNIZED_SHAPE` | 200 OK but the body did not match a known candle shape (vendor key drift). The redacted body is in the note — **not** proof of "no data". |
| `UNAUTHORIZED (401 …)` | Token invalid or expired — re-mint / re-login (Dhan; Kite uses 403 for this). |
| `FORBIDDEN (403 …)` | Kite: token invalid/expired (`error_type=TokenException`, surfaced in the verdict) or entitlement. Dhan: Data-API not subscribed. |
| `RATE_LIMITED (429)` | Hit the rate cap (the probe already paces below it). |
| `TRANSPORT_ERROR` | Network/DNS/timeout — host unreachable from this machine. |
| `MINT_OK` / `MINT_DENIED` / `MINT_FAILED` | Headless PIN+TOTP mint outcome. |
| `SKIPPED (...)` | No creds / no token / budget spent / after auth failures. |

To answer the gates:

* **Dhan securityId confirm (g):** the three `scrip-confirm: …` rows — all
  `CONFIRMED` means the hardcoded engine ids (NIFTY=13 / BANKNIFTY=25 / SENSEX=51)
  are correct; any `MISMATCH` / `NOT-FOUND` is a **loud** stop-and-fix (a wrong id
  pulls the wrong instrument). `UNCONFIRMED-CSV-UNREACHABLE` = try again from a
  machine that can reach `images.dhan.co`.
* **Dhan floor (a):** find the oldest `CANDLES` window per index. If 2020/2021
  windows are `EMPTY` but 2023/recent are `CANDLES`, the "last 5 years" rolling
  policy is confirmed and Dhan intraday does **not** reach the `2020-01-01` floor.
* **Dhan VIX (b):** the `INDIA VIX … @ recent` row — `CANDLES` = served.
* **Dhan mint (c):** the `pin_totp_mint` row's verdict + `response keys` note.
* **Dhan SENSEX/BSE spot (e):** the `SENSEX @ recent` and `SENSEX @ 2022-06` rows
  — `CANDLES` on either settles that Dhan serves BSE index 1-minute.
* **Dhan rollingoption (f):** the two `rollingoption: … ATM CALL WEEK` rows —
  `CANDLES (N) · fields[… volume …]` confirms rollingoption serves candles **with
  volume** (and whichever of oi / iv / spot appear); the `SENSEX` case doubling as
  the `BSE_FNO`-servability proof. `FORBIDDEN (403 …)` here = the Data-API /
  options entitlement is missing.
* **Kite SENSEX (d):** the `SENSEX 265 @ recent` row — `CANDLES` settles it.

A human summary also prints to stdout at the end; the machine-readable evidence is
the file you passed to `--evidence-out`.

---

## 5. Security note

* Secrets are read **only** from environment variables — never a flag, file, or
  prompt.
* Every string that is printed or written passes through a redactor seeded with
  the client id, PIN, TOTP secret, computed TOTP, minted/supplied access token,
  api key, and Kite access token, so an accidental echo (even inside a vendor
  error body or URL) is masked to `***redacted***`.
* The tool is strictly **read-only**: it calls only the public scrip-master CSV
  (GET), the historical/intraday/rollingoption **read** endpoints (all
  `POST /charts/*` reads), and the one optional token mint. The scrip-master
  **ID-confirm (goal g)** issues **no** network call of its own — it re-parses the
  one CSV already fetched — and the public CSV carries no secret. It never touches
  an order, trade, modify, or IP-write endpoint. The `/charts/rollingoption` body
  carries no secret (securityId + segment + dates); the SDK-injected
  `dhanClientId` is the client id, which the redactor already masks.
* The emitted evidence file is therefore safe to commit alongside this runbook.
