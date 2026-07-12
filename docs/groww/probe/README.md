# Groww Phase-0 probe transcripts

`DEC-DHAN-GROWW-REMIGRATE-001` (ADR-0009 §3.2 / §4). This directory holds the
committed output of `scripts/groww_probe.py`, the empirical hard-gate the
Dhan → Groww re-migration is conditional on.

The probe **cannot run in the build sandbox** (no Groww credentials, no network
egress). The operator runs it on a machine with Groww credentials and commits
the transcript here **before** any live adapter code (PR-B onward) lands:

```
BRUTEX_GROWW_API_KEY=... BRUTEX_GROWW_TOTP_SECRET=... \
  python scripts/groww_probe.py --all | tee docs/groww/probe/probe-$(date +%F).txt
```

> **Scope note (2026-07-06, DEC-BROKER-DOCS-AWSONLY-RECONCILE-001):** the
> `BRUTEX_GROWW_API_KEY` / `BRUTEX_GROWW_TOTP_SECRET` env vars above are PROBE-LOCAL
> to the standalone Phase-0 `scripts/groww_probe.py` — read by that script only,
> never by the production pull path, whose credentials are AWS-SSM-only
> (DEC-AWS-ONLY-CREDS-001, MIGRATION.md #1065; `docs/runbooks/ENV_VARS.md` §5).
> Retiring/repointing the probe at SSM is a NAMED follow-up, not done here.

The transcript must resolve the six hard-gates (ADR-0009 §4):

1. India VIX existence on Groww — the HARD-GATE; if absent, ADR-0009 §7
   reversal applies (keep Dhan).
2. 1-minute granularity at the 2020 boundary (COVID window).
3. Backtest-vs-live parity (measures the withdrawn folklore claim).
4. Cross-vendor 1m drift Dhan ↔ Groww.
5. Backtesting rate limit / daily quota / unattended token lifetime.
6. Options round-trip (`get_expiries` → `get_contracts` → candles).

Until a transcript lands here, every Groww fact marked UNVERIFIED in ADR-0009
stays UNVERIFIED and is not encoded as fact in adapter code (Golden Rule #1).

## Post-close settle probe (DEC-GROWW-POSTCLOSE-SETTLE-PROBE-001)

`postclose_settle_probe.py` + `POSTCLOSE_SETTLE_RUNBOOK.md` measure WHEN Groww
settles TODAY's 1-minute day (the #837 forming shape → settled 7-element rows) —
the evidence base for the pre-settle TODAY gate (DEC-PULL-TODAY-COMPLETENESS-GATE-001,
MIGRATION.md #1751/#1752). Transcripts land here as
`postclose-<YYYY-MM-DD>-<HHMM>.jsonl` + the tee'd `.txt` tables. Unlike the Phase-0
script above it reads its credentials + the shared read-only token from AWS SSM
(the doctor-kit client — mint physically blocked, fully paced), never probe-local
env vars.
