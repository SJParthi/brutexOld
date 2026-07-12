# Runbook — DhanHQ v2 auth (daily token, AWS-SSM only)

**Decision:** `DEC-DHAN-AUTH-001` (ADR-0020, MIGRATION.md #1755). AUTH ONLY — no
fetch (PR-3). Secret safety is paramount: credentials + the minted token live
ONLY in AWS Parameter Store SecureStrings, never an env var / file / webpage, and
never a log / audit line / exception message.

---

## 1. What this covers

BruteX self-mints the daily DhanHQ v2 access token via the headless PIN+TOTP flow
and stores it back to a single SecureString. The token has a 24-hour lifetime;
BruteX refreshes it at 20 h (a 4 h margin) via RenewToken, falling back to a fresh
mint. All of this is unattended (zero clicks/day) once the SSM parameters + IAM
grant are in place.

## 2. SSM parameters (seed these ONCE)

Region: `ap-south-1` (the shared token-broker region; override with
`BRUTEX_TOKEN_AWS_REGION`). All are **SecureString**.

| Parameter | Contents | Env override |
|-----------|----------|--------------|
| `/tickvault/prod/dhan/client-id` | numeric Dhan client id | `BRUTEX_TOKEN_PARAM_DHAN_CLIENT_ID` |
| `/tickvault/prod/dhan/pin` | 6-digit login PIN | `BRUTEX_TOKEN_PARAM_DHAN_PIN` |
| `/tickvault/prod/dhan/totp-secret` | Base32 TOTP shared secret | `BRUTEX_TOKEN_PARAM_DHAN_TOTP_SECRET` |
| `/tickvault/prod/dhan/access-token` | **written by BruteX** (the minted token envelope) | `BRUTEX_TOKEN_PARAM_DHAN_ACCESS_TOKEN` |

Seed the three credential parameters (client-id / pin / totp-secret); BruteX
writes the access-token parameter itself.

```bash
aws ssm put-parameter --region ap-south-1 --type SecureString --overwrite \
  --name /tickvault/prod/dhan/client-id --value '<numeric client id>'
aws ssm put-parameter --region ap-south-1 --type SecureString --overwrite \
  --name /tickvault/prod/dhan/pin --value '<6-digit pin>'
aws ssm put-parameter --region ap-south-1 --type SecureString --overwrite \
  --name /tickvault/prod/dhan/totp-secret --value '<base32 totp secret>'
```

The `BRUTEX_DHAN_RENEW` env kill switch (default ON; junk fails loud) disables the
RenewToken optimization — with it off, every refresh is a fresh mint.

## 3. IAM (BruteX's first-ever SSM WRITE — least privilege)

BruteX reads the credential triple and reads+WRITES only the one access-token
parameter. Grant EXACTLY this (the Groww namespace stays read-only — do NOT widen
it):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DhanCredsRead",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter"],
      "Resource": "arn:aws:ssm:ap-south-1:<ACCOUNT_ID>:parameter/tickvault/prod/dhan/*"
    },
    {
      "Sid": "DhanTokenWrite",
      "Effect": "Allow",
      "Action": ["ssm:PutParameter"],
      "Resource": "arn:aws:ssm:ap-south-1:<ACCOUNT_ID>:parameter/tickvault/prod/dhan/access-token"
    },
    {
      "Sid": "DhanKms",
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:Encrypt"],
      "Resource": "<the KMS key ARN backing the SecureStrings>"
    }
  ]
}
```

`kms:Encrypt` is required only for the write (the `PutParameter` SecureString).
The KMS key choice is a **manual-acceptance item** (use the account's SSM default
key, or a dedicated CMK — the operator decides).

## 4. Wet-run gates (before MERGE — not before build)

The build ships fully with the SSM-write posture, but two operator confirmations
are required before merge (ADR-0020 §2.1 / §4):

1. **Exclusivity** — confirm TickVault (or any other consumer) runs NO live Dhan
   feed. BruteX self-minting a fresh Dhan token could invalidate a token another
   consumer is using (single-active-token behaviour is UNVERIFIED); the whole
   self-mint stance assumes BruteX is the sole Dhan-token user.
2. **Write grant** — provision the `ssm:PutParameter` grant on
   `.../dhan/access-token` (§3) so `brutex dhan-token mint` can persist.

## 5. Operate

```bash
brutex dhan-token status   # prints age / remaining / sha256[:8] — NEVER the token; exit 0 fresh, 1 stale/absent
brutex dhan-token mint     # force a fresh headless PIN+TOTP mint + store
brutex dhan-token renew    # renew the active token + store
```

`status` is safe to run anytime (read-only). `mint`/`renew` write the SecureString.

## 6. Lockdown + recovery

The reauth spine LATCHES a lockdown after ONE invalid-credential rejection, or
after `DHAN_MINT_LOCKDOWN_THRESHOLD` consecutive terminal failures. Once latched,
every auth call fails fast with ZERO HTTP and ONE CRITICAL Telegram fires. There
is **no env escape hatch** — recovery is a deliberate operator action:

1. Verify the credential triple in SSM (wrong PIN / TOTP secret / client id is the
   usual cause) — re-seed the offending parameter (§2).
2. Verify the Dhan account preconditions (TOTP-2FA enabled, an active Data-API
   subscription if required — UNVERIFIED, see ADR-0020 §4).
3. Restart the process (the lockdown is in-memory; a restart clears it).

## 7. Live-probe TODO (flip the `_UNVERIFIED` sentinels)

The four `_UNVERIFIED` constants in `brutex/adapters/dhan/constants.py` record
what Phase-0 could not confirm live (mint preconditions, auth-endpoint rate limit,
renew-requires-active-token, mint-invalidates-prior-token). A credentialed live
probe against a real Dhan account should confirm each and update those constants +
this runbook. Until then they are observe-only and gate nothing.
