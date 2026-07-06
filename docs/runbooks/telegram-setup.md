# Runbook — Telegram Alert Channel Setup

> **Target audience:** BruteX operator (you)
> **Time required:** ~5 minutes
> **Prerequisites:** Telegram app installed on your phone or desktop
>
> This runbook implements `FR-ING-016` and `§15.6 Telegram Alert Channel`
> from `docs/REQUIREMENTS.md`. Follow it once per environment (you can
> share the same bot across dev/prod if you want).

---

## What you're about to do

1. Create a new Telegram bot via **@BotFather** (Telegram's official bot for making bots)
2. Get two secrets: a **bot token** and a **chat ID**
3. Put those secrets into BruteX's environment variables
4. Send a test message to confirm everything works

After this, BruteX will send you real-time ingest progress, quarantine
alerts, and rate-limit anomalies during long-running runs.

---

## Step 1 — Create the bot (2 minutes)

1. Open Telegram (phone or desktop).
2. Search for **`@BotFather`** and open the chat. It has a blue verified checkmark.
3. Send `/newbot` (or tap it from the menu).
4. **BotFather asks for a display name.** This is what appears at the top of the chat.
   Example: `BruteX Ingest`
5. **BotFather asks for a username.** Must be unique across Telegram and must end with `bot`.
   Example: `sjparthi_brutex_bot` or `brutex_sjp_bot` — pick something personal so it's unique.
6. BotFather replies with a message containing the **bot token**. It looks like:

   ```
   123456789:ABCdefGHIjklMNOpqrSTUvwxYZ-1234567890abc
   ```

   **Copy this token and save it somewhere temporary (a scratch note).** You'll put it into
   an env var in Step 3. **Treat it like a password — never commit it to git, never paste
   it in public chats.**

7. Optional cosmetics:
   - `/setdescription` → "BruteX ingest pipeline alerts"
   - `/setuserpic` → upload a BruteX logo if you have one
   - `/setcommands` → (skip for now, we don't need bot commands for alerts)

---

## Step 2 — Get your chat ID (2 minutes)

The bot needs to know **which chat to send messages to**. For personal alerts,
this is your own DM with the bot.

### Option A — direct message (recommended for single-operator use)

1. In Telegram, search for the bot you just created (e.g. `@sjparthi_brutex_bot`).
2. Open the chat and **press `/start`**. This is important — bots cannot
   send unsolicited messages, so you need to initiate the chat first.
3. In a browser, open this URL (replace `<BOT_TOKEN>` with your token from Step 1):

   ```
   https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
   ```

   Example:

   ```
   https://api.telegram.org/bot123456789:ABCdef.../getUpdates
   ```

4. You'll see a JSON response like:

   ```json
   {
     "ok": true,
     "result": [
       {
         "update_id": 123456789,
         "message": {
           "message_id": 1,
           "from": { "id": 987654321, "first_name": "Parthiban", ... },
           "chat": { "id": 987654321, "type": "private", ... },
           "date": 1713090000,
           "text": "/start"
         }
       }
     ]
   }
   ```

5. Find `"chat": { "id": 987654321 ... }`. **That number (e.g., `987654321`) is your chat ID.**
   For direct messages, the chat ID equals your Telegram user ID.

6. **Save the chat ID** alongside the bot token.

### Option B — group chat (if you want a team channel)

1. Create a new Telegram group and add your bot to it.
2. Send any message in the group (e.g., `/start@sjparthi_brutex_bot`).
3. Hit the `getUpdates` URL as above.
4. In the JSON, find `"chat": { "id": -1001234567890, "type": "supergroup" ... }`.
   The **negative number** is the group chat ID. Copy that.
5. **Important:** in Telegram groups, bots only see messages that start with `/` or
   `@botname`, unless you disable privacy mode via `/setprivacy` in BotFather. For
   BruteX's purposes, the bot only needs to SEND messages, not read them, so privacy
   mode can stay on.

---

## Step 3 — Wire the secrets into BruteX

Add these three environment variables to your shell profile (`~/.zshrc`, `~/.bash_profile`, etc.)
or to a local `.env` file that is **never committed to git**:

```bash
export BRUTEX_TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ-1234567890abc"
export BRUTEX_TELEGRAM_CHAT_ID="987654321"
export BRUTEX_TELEGRAM_ENABLED="true"
```

Then reload your shell:

```bash
source ~/.zshrc    # or the appropriate rc file for your shell
```

**Verify:**

```bash
echo "$BRUTEX_TELEGRAM_BOT_TOKEN" | head -c 20  # should print the first 20 chars, not empty
echo "$BRUTEX_TELEGRAM_CHAT_ID"                 # should print your chat ID
echo "$BRUTEX_TELEGRAM_ENABLED"                 # should print 'true'
```

### ⚠️ Security rules (non-negotiable)

These are enforced by `NFR-SEC-001` and the `.gitignore`:

1. **NEVER commit `BRUTEX_TELEGRAM_BOT_TOKEN` to git.** It is equivalent to a password.
2. **NEVER paste the token in a screenshot, public chat, or bug report.** If leaked,
   immediately revoke it via BotFather (`/revoke` → pick bot → get new token).
3. **NEVER print the full token to logs.** BruteX's structlog PII filter redacts tokens
   matching `^\d+:[A-Za-z0-9_-]{20,}$` automatically.
4. **Rotate quarterly** or after any suspected leak.

The `.gitignore` in this repo already blocks `.env*` files, `*token*`, and
`**/api_token*`. Don't override those rules.

---

## Step 4 — Send a test message

Once the code for the notifier is written (in a subsequent commit), you'll be
able to run:

```bash
brutex notify test
```

Which sends a single test message:

```
🧪 BruteX Telegram test — configuration OK
    run_id: test-<uuid>
    host: <hostname>
    time: 2026-04-14T12:34:56Z
```

If you get the message in Telegram, you're done. If not, the most common
failures are:

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` | Bot token wrong or typo'd | Re-copy from BotFather, check for trailing whitespace |
| `400 Bad Request: chat not found` | Chat ID wrong or you forgot `/start` | Re-run getUpdates, ensure you sent /start first |
| `403 Forbidden: bot was blocked by the user` | You blocked the bot by mistake | Unblock in Telegram settings |
| No message, no error | `BRUTEX_TELEGRAM_ENABLED` is not `true` | Check the env var — must be literal string `true` |
| SSL / network error | Firewall blocks `api.telegram.org` | Check corporate firewall or VPN |

---

## How the notifier behaves during ingest

Once wired, you'll start receiving messages automatically. Typical Phase D
(24-hour 1-minute candle fetch) sends roughly **440 messages**:

- 1 × `run.start`
- 4 × `phase.start` and 4 × `phase.complete`
- ~288 × `phase.progress` (every 5 minutes during Phase D)
- ~144 × `run.heartbeat` (every 10 minutes)
- 0-20 × `quarantine.new` if anything goes wrong
- 0-5 × `rate_limit.downshift` during broker peak load (Groww adaptive [4..8] r/s)
- 1 × `run.complete`

If that's too chatty, you can mute the chat in Telegram (long-press → mute)
and the messages still get delivered — you just don't get push notifications.
For quarantine alerts specifically, you can set a custom sound or keyword
notification so the important ones still break through.

---

## Revoking / rotating the bot token

If you think the token has leaked:

1. Open `@BotFather` → `/mybots` → select your bot → `API Token` → `Revoke current token`
2. Copy the new token
3. Update `BRUTEX_TELEGRAM_BOT_TOKEN` in your shell profile
4. Reload shell
5. Send a test message to confirm

The old token becomes invalid immediately. Any running BruteX process will
start getting `401 Unauthorized` on its next Telegram send and will log the
error (but **not halt the ingest** — Telegram failures are tolerated per
§15.6 Delivery semantics).

---

## Deleting the bot entirely

If you want to kill the bot:

1. Open `@BotFather` → `/mybots` → select your bot → `Delete bot` → confirm

The bot is gone. Any BruteX process still trying to use the old token will
get errors and they'll surface in structlog but not affect ingest correctness.

---

## References

- `docs/MIGRATION.md` — telemetry event taxonomy is tracked in the
  ledger; the prior `REQUIREMENTS.md §15.6` was retired on 2026-05-15
- Telegram Bot API docs — https://core.telegram.org/bots/api (referenced by the operator, not fetched by BruteX)

---

## End-to-end verification with `compose_standard_monitor` (DEC-STANDARD-MONITOR-001)

After the env vars are set, run the canonical smoke test:

```bash
python scripts/ops/test_telegram.py
```

Expected output:

```
Sending test event to Telegram chat <CHAT_ID> via bot ...
✓ Telegram event published; check your chat now.
```

Your phone should receive a message containing
"BruteX Telegram smoke test — env wired correctly."

If you want to verify the FULL monitor chain (SLA + Watchdog + Telegram):

```bash
brutex monitor --ticks 3 --poll-interval 0.5
```

This runs 3 ticks of the production composer (`compose_standard_monitor`),
which wires the Telegram callback. The default SLA rule list is empty,
so no alert fires unless you provide explicit `SLARule` objects via a
Python entry point — see `brutex/observability/standard_monitor.py`
docstring for the YAML-loader roadmap.

---

## CI Actions secrets (optional — doctor-daily Telegram alert)

The daily `brutex doctor` cron (`.github/workflows/doctor-daily.yml`,
DEC-DOCTOR-DAILY-CRON-001) can additionally send a best-effort Telegram
alert when a run goes red. To enable it, provision two **repository
Actions secrets** (GitHub → Settings → Secrets and variables → Actions)
with the SAME values as the app env vars above:

| Actions secret | Value |
|---|---|
| `BRUTEX_TELEGRAM_BOT_TOKEN` | the BotFather bot token from Step 1 |
| `BRUTEX_TELEGRAM_CHAT_ID` | the chat ID from Step 2 |

This is an **operator task and never required for green**: when the
secrets are absent the workflow's Telegram step degrades gracefully to a
skip notice (exit 0) and the GitHub tracking issue remains the
guaranteed alert channel. The secret NAMES mirror the app env vars and
are already allowlisted by name in `.gitleaks.toml`.

---

_Last updated: 2026-07-06 (added the optional CI Actions secrets section for the doctor-daily cron per DEC-DOCTOR-DAILY-CRON-001; earlier 2026-05-25 — compose_standard_monitor verification per DEC-STANDARD-MONITOR-001)._
