# BruteX — Claude Code Project Rules

> Persistent instructions for Claude Code working in this repository.
> Every Claude session in `bruteX/` must read and honor this file.
>
> **Authority order:**
> 1. This file (`CLAUDE.md` — 12 Golden Rules)
> 2. Defaults
>
> The previous DEC-PROTOCOL-001 layer (tickvault adaptation doc +
> auto-loaded protocol bundle) remains RETIRED (DEC-P0-026, PR #48).
> The ONLY co-development surface re-established since is the shared
> `rust/trading-core/` crate — the single source of truth for the
> evaluate-time indicators (pivots + Fibonacci) + the 52-condition
> vocabulary, consumed bit-identically by BOTH BruteX (via the PyO3
> `trading_core` wheel) and TickVault (native Rust) per
> DEC-SHARED-CORE-001 (MIGRATION.md #441). The frozen 400-case golden
> answer-key is the contract both sides satisfy; no other
> adaptation/protocol coupling exists.

