# BruteX — Claude Code Project Rules

> Persistent instructions for Claude Code working in this repository.
> Every Claude session in `bruteX/` must read and honor this file.
>
> **Authority order:**
> 1. This file + the Golden Rules — full verbatim text in
>    `docs/claude/04-golden-rules.md` (MANDATORY read before any work)
>    Session start: also honor docs/claude/12-session-etiquette.md (old §10 — it mandates reading docs/TRACK1_BACKTEST_SPEC.md + docs/MIGRATION.md at session start).
> 2. Defaults
>
> **2026-07-18 restructure:** the previous 1,026 KB single-file CLAUDE.md
> was split. This root is a lean charter + index; EVERY line of the old
> file was moved VERBATIM into `docs/claude/` — the in-order
> concatenation of the 13 files there is byte-identical to the old file
> (sha256 `32b923432d2203fb3d24d5d88ea2a2c814cde18508e637d0f5e23b74b2964af4`).
> Read topic files on demand only — never bulk-read them at session start.

## Identity (summary)

- Full identity + scope: `docs/claude/02-project-identity.md`.
- The ONLY tickvault co-development surface: the shared
  `rust/trading-core/` crate — single source of truth for the
  evaluate-time indicators (pivots + Fibonacci) and the 52-condition
  vocabulary, consumed bit-identically by BOTH BruteX (via PyO3) and
  tickvault. Any change to this crate is cross-repo per
  DEC-SHARED-CORE-001: both consumers must stay bit-identical, and the
  frozen 400-case golden answer-key is the contract both sides satisfy.

## The 13 Golden Rules (index — the moved file is authoritative)

Full verbatim text: `docs/claude/04-golden-rules.md` (old CLAUDE.md §2).

1. **No hallucination** — every factual Groww/NSE/BSE/NIFTY claim is verified, never guessed.
2. **Single source of truth** — all Track 1 specs live in their one designated home.
3. **Scope lock** — Track 1 = Groww + spot indexes + India VIX; nothing else creeps in (India VIX reference-only).
4. **Reproducibility first** — every run is identified by a blake3 hash of its inputs.
5. **O(1) discipline** — bar lookup, indicator value lookup, combo dedup are O(1).
6. **Rate-limit safety** — respect Groww's published type-level rate limits; never out-poll; historical-candles caps UNDOCUMENTED.
7. **Idempotence** — every ingest chunk and every backtest combo is safely re-runnable.
8. **Audit trail** — every material action (ingest chunk, combo run, …) leaves a record.
9. **Hostile review before code** — major design changes get adversarial review first.
10. **Honest limits** — if an SLA cannot be met under current constraints, say so plainly.
11. **COMMON · DYNAMIC · SCALABLE · AUTOMATED** — operator decree 2026-05-16.
12. **100% ALWAYS STANDARDS** — operator decree 2026-05-16 evening.
13. **MERGE-READINESS CHARTER** — operator decree 2026-05-29.

## Topic index (old § → new home; read when working on that topic)

| Old location | File | Read when |
|---|---|---|
| header ledger | `docs/claude/01-header-changelog-ledger.md` | decision archaeology (DEC-* history) |
| §1 | `docs/claude/02-project-identity.md` | scope questions |
| §1 branch history | `docs/claude/03-active-branch-history.md` | decision archaeology |
| §2 | `docs/claude/04-golden-rules.md` | ALWAYS — mandatory |
| §3 | `docs/claude/05-branching-and-commits.md` | any commit / PR |
| §4 | `docs/claude/06-coding-standards.md` | writing code |
| §5 | `docs/claude/07-directory-conventions.md` | adding files |
| §6 | `docs/claude/08-verified-groww-facts.md` | Groww API work |
| §7 | `docs/claude/09-market-microstructure-facts.md` | market/session logic |
| §8 | `docs/claude/10-hardware-envelope.md` | perf / SLA claims |
| §9 | `docs/claude/11-when-in-doubt.md` | uncertainty |
| §10 | `docs/claude/12-session-etiquette.md` | session protocol |
| original preamble | `docs/claude/00-original-preamble.md` | provenance |

## Shared indicators + strategies (both repos)

`rust/trading-core/` is THE shared surface with tickvault: evaluate-time
indicators (pivots + Fibonacci) + the 52-condition vocabulary,
bit-identical in both consumers. Any change there is cross-repo —
coordinate per `docs/claude/04-golden-rules.md`.

## Citation note (for existing comments across CI/scripts)

Comments citing "CLAUDE.md §N" resolve via the table above — the §
numbering is preserved verbatim inside the moved files. Legacy "§11"
citations mean Golden Rule #11 (COMMON · DYNAMIC · SCALABLE · AUTOMATED)
in `docs/claude/04-golden-rules.md`. The archived preamble's "12 Golden
Rules" count is stale — 13 is correct (rule 13 = MERGE-READINESS CHARTER,
operator decree 2026-05-29).
Per-PR ledger updates (the "_Last updated" header entry + the Active
branch blurb) now go to docs/claude/01-header-changelog-ledger.md +
docs/claude/03-active-branch-history.md - the root stays stable.
