# ADR-0020 — Re-admit DhanHQ v2 as a Second Data Source (RUST-FIRST auth exemption)

**Decision ID:** `DEC-DHAN-AUTH-001` (MIGRATION.md entry #1755)
**Date:** 2026-07-12
**Status:** ACCEPTED (Dhan re-admitted as a **feed track**) — **PRODUCTION CUTOVER GATED** on the operator's Dhan↔TickVault account-exclusivity confirmation (pending as of this ADR).
**Author:** Claude Code (operator-directed)
**Supersedes:** **ADR-0009 §5 (in part)** — the ADR-0009 "delete-Dhan-last" cutover
removed the Dhan adapter package in the J4 PR series and installed the
`no-dhan-guard` to forbid the vendor word on the live surface. The operator's
2026-07-12 decision RE-ADMITS Dhan as a **second** data source alongside Groww
(Groww stays primary); ADR-0009 §5's Dhan-removal is thereby superseded, the
guard is retired (`DEC-DHAN-GUARD-RETIRE-001`, #1753), and a new AUTH adapter is
built. ADR-0009's Groww migration itself stands — this ADR does NOT revert to
Dhan, it ADDS Dhan beside Groww.
**Governance provenance (Golden Rule #1) — what IS on record vs what is NOT.**
Two things are authorized on record: (1) the **independent-feeds task** this PR
series serves, and (2) the operator's **2026-07-12 re-admission of Dhan as a
sanctioned vendor** ("use both dhan and groww"), which supersedes ADR-0009 §5's
Dhan-removal. Together these authorize the **feed track** — re-admitting Dhan
beside Groww and BUILDING its auth adapter. What is **NOT** yet authorized is
**production cutover**: live self-minting against a possibly-shared account, the
SSM `PutParameter` write in production, and using Dhan data in production. That
is GATED on the operator's **pending Dhan↔TickVault account-exclusivity
confirmation**. No vendor claim below is derived from anything but the operator
directive + the Phase-0 first-party evidence (`scratchpad/phase0-dhan-auth.md`,
verified against the official `dhan-oss/DhanHQ-py` SDK).

**This PR BUILDS the mint + SSM-write capability; it does NOT put it into
production.** MERGE and any production use are HELD until the operator confirms
Dhan is a BruteX-independent account (or that minting is isolated/safe). This ADR
does NOT claim the operator approved self-minting against the shared-credential
model — only that Dhan is re-admitted as a feed track and the capability is built
behind the pre-merge exclusivity gate.

---

## 1. Context

Track 1's live vendor is Groww (ADR-0009). The operator wants a SECOND
historical-data source (DhanHQ v2) for redundancy + coverage. PR-1 (this ADR)
delivers the AUTH control plane ONLY — the daily access-token lifecycle. The
data plane (fetch / candles / rate caps) is PR-3 and is deliberately out of
scope here.

DhanHQ v2 supports a **headless PIN+TOTP mint**
(`POST https://auth.dhan.co/app/generateAccessToken?dhanClientId&pin&totp`,
VERIFIED in the official SDK) and a **RenewToken** chain
(`GET https://api.dhan.co/v2/RenewToken`, headers only), with a 24-hour token
lifetime (effective 2025-10-01 per SEBI algo-trading guidelines; PARTIAL
provenance — see §4). This is the SAME automation class as the existing
AWS-minted Groww TOTP token, so an unattended (zero-click) daily token is
feasible.

## 2. Decision

1. **Retire the `no-dhan-guard`** (`DEC-DHAN-GUARD-RETIRE-001`): delete the
   script + all CI wiring. Post-retirement `dhan` is allowed on the live surface.
2. **Extract the AWS-SSM + secret-scrub seams as COMMON**
   (`DEC-AWS-SSM-SEAM-COMMON-001`): move-with-shim, Groww byte-identical (the
   Groww suites are the oracle). A NEW write-capable `AwsClientWriter` Protocol is
   added; the read-only Groww `AwsClient` is NOT widened (write-incapability stays
   a security property).
3. **Build the `brutex/adapters/dhan/` AUTH adapter** (`DEC-DHAN-AUTH-001`): mint
   / renew / SecureString token store / thread-safe single-flight reauth spine /
   hash-chained audit — all AWS-SSM-only, secret-safe, 100% line+branch.

### 2.1 SELF-MINT stance (inverse of Groww)

Groww's shared token is minted by a dedicated Lambda; BruteX is a strictly
read-only consumer (`readonly=True`). **Dhan is assumed BruteX-EXCLUSIVE**, so
BruteX self-mints the daily Dhan token and WRITES it back to
`/tickvault/prod/dhan/access-token` — the store's `readonly=False`, and BruteX's
IAM role gains its FIRST-EVER SSM write (`ssm:PutParameter` on **exactly** that
one parameter; the Groww namespace stays read-only). The SSM-write posture is the
security hinge: **the operator confirms Dhan exclusivity (TickVault runs no live
Dhan feed) and provisions the narrow write grant BEFORE MERGE**, not before build.

### 2.2 Secret safety (paramount)

Creds (client-id / PIN / TOTP-secret) + the minted token come ONLY from AWS SSM
SecureStrings — never env / file / webpage. The headless mint carries the secret
PIN + TOTP in the URL QUERY STRING, and a urllib `HTTPError.__str__` embeds that
URL — so EVERY transport exception is caught at the auth boundary
(`brutex/adapters/dhan/auth.py`) and re-raised **scrubbed** (secrets masked + URL
query redacted, `from None` so the raw object never rides `__cause__`). The error
hierarchy scrubs AT CONSTRUCTION, so a leaky auth error is structurally
impossible. Logs + audit carry only `sha256(token)[:8]`. Cross-process mint-storm
is braked by an advisory `flock` + a double-checked SSM re-read.

## 3. RUST-FIRST exemption (DEC-RUST-FIRST-MANDATE-001)

The auth control plane executes **≤ 3 HTTPS calls per day** (one mint or renew
per 24 h window + a rare mid-pull reauth) — nothing per-bar or per-request-hot, so
no O(1) claim is made and no bench ships (Golden Rule #10). It composes `boto3` +
`pyotp`, exactly the vendor-SDK exemption class, mirroring the proven 100%-covered
Groww Python auth stack. The seam PR-3 consumes is `current_token() -> str` — a
plain header string a Rust fetch/parse core ingests with zero Python round-trips
per fetch — so nothing here precludes PR-3's Rust data plane. Python auth is
therefore justified as a once-daily control-plane path under the RUST-FIRST
mandate.

## 4. Honest envelope (Golden Rule #10)

VERIFIED (first-party): the mint / renew endpoint SHAPES + the header names
(official SDK). PARTIAL / UNVERIFIED, carried as `_UNVERIFIED` sentinels that no
auth DECISION branch may import (AST-pinned):

* the PIN+TOTP mint **preconditions** (whether TOTP-2FA must be pre-enabled or an
  active Data-API subscription is required) — Phase-0 could not confirm live;
* whether the mint endpoint is **rate-limited/throttled**;
* whether **RenewToken requires a still-active token** (believed True, encoded as
  the conservative assumption, drives nothing);
* whether a **fresh mint INVALIDATES the prior token** (single-active-token
  behaviour) — UNKNOWN, which is precisely why the cross-process SSM re-read brake
  is mandatory before a mint.

Also honest: the mint URL exposes the PIN + TOTP to Dhan's own edge (a
vendor-side exposure inherent to the documented flow — mitigated on OUR side by
never logging/auditing the raw URL). **Wet-run gates (before MERGE, not build):**
the operator confirms TickVault runs no live Dhan feed (exclusivity) AND
provisions the `ssm:PutParameter` write grant on `.../dhan/access-token`.

Named follow-ups (NOT claimed done): the dedicated secret-leak red-team battery +
the shim-identity + guard-reconcile integration proofs (next agent); wiring the
new modules into the weekly `mutation.yml` gate (deferred here to avoid touching
the mutation workflow's trigger/budget shape — so this ADR does NOT claim the
modules "ride the weekly mutation workflow", per the #1614 honesty lesson); the
data plane (PR-3).

## 5. Consequences

* `dhan` is allowed on the live surface again; the guard is gone.
* BruteX performs its first SSM write (narrow, one parameter).
* Groww is UNAFFECTED (byte-identical; the shared seams are identity-pinned).
* ENGINE UNCHANGED — NIFTY / BANKNIFTY / SENSEX.

## 6. References

* `docs/MIGRATION.md` #1753 / #1754 / #1755.
* `scratchpad/phase0-dhan-auth.md` (Phase-0 research).
* Official SDK: `dhan-oss/DhanHQ-py` (`src/dhanhq/auth.py`).
* `docs/runbooks/dhan-auth.md` (operator runbook — IAM policy + wet-run drill).
* ADR-0009 (the Groww migration this ADR builds beside, superseding only its §5
  Dhan-removal).
