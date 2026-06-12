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
