# CLAUDE.md conflict-hunk resolution — club X refold onto post-club-P main

**Scope:** the ONE conflict `git merge` produces when merging post-club-P main
(main d6ac9f85 + PR #1547 squash of `integration/club-p-pull` @ 2bd0f89d) into
`integration/club-x-dhan` (tip b6341fc as of the 2026-07-15 dry run): the §6
"Adapter effective cap" table row. Dry-run-verified: exactly ONE hunk, markers
at CLAUDE.md line 8398 (`<<<<<<< HEAD` = club X / Dhan side; the other side =
post-P main). Everything else auto-merges (docs/MIGRATION.md then needs the
sibling `renumber.sh` — the textual merge double-claims entry #1795).

## Resolution law (three parts)

1. Take the **post-P (theirs) side** as the base — it carries club P's moved
   citations (`dashboard/pull_controller.py:1026-1027`; `cli.py:...`).
2. Fold in the Dhan side's **"(injected at the CLI live build site)"** wording,
   attached to the `cli.py` citation (on the Dhan side it annotated the CLI
   live-build-site citation; post-P renders that site as a `cli.py` line cite).
3. **Repoint the `cli.py` line numbers to the MERGED tree**: post-P's
   `cli.py:5271-5272` is accurate on club P alone, but club X's Dhan CLI code
   above that point shifts the live build site to **`cli.py:5484-5485`**
   (`ceiling_rps=` / `ceiling_per_min=` injection) — dry-run-proven: the
   citation-accuracy scanner FAILS on `:5271-5272` in the merged tree
   ("found GROWW_RATE_LIMIT_RPS_CEILING at :5459,5467,5484; ceiling_per_min
   at :5485; ceiling_rps at :5484") and passes on `:5484-5485`.
   `pull_controller.py:1026-1027` needs NO repoint (scanner-verified accurate
   in the merged tree). If the branches move before execution, re-run the
   scanner and repoint to its printed "found at" lines (±5 tolerance).

Everything before the citations column is byte-identical on both sides.

## Exact final text of the resolved row (ONE line — no wrapping; 637 chars)

```
| Adapter effective cap | **Vendor-neutral adaptive token bucket** — injected `ceiling_rps` (the production live ceiling is the operator-DIRECTED **8 r/s**, `GROWW_RATE_LIMIT_RPS_CEILING`, DEC-GROWW-RATE-CEILING-8-002; the vendor-neutral DEFAULT stays 5), steps down to a 1 r/s floor on 429/timeout and recovers to the ceiling once clear — PLUS an injected `ceiling_per_min` HARD 500/min rolling-window governor on top (DEC-GROWW-RATE-PERMIN-001) | `adapters/rate_common.py:372-426`; `groww/constants.py:177`; `dashboard/pull_controller.py:1026-1027`; `cli.py:5484-5485` (injected at the CLI live build site) + this file §2 rule 6 |
```

(Also stored raw, newline-terminated, at `_resolved_row.txt` beside this file —
prefer copying from there to avoid any editor re-wrapping.)

## Surrounding context (unchanged lines — for unambiguous placement)

The resolved row replaces the whole conflict block. It sits between the
"Rate limit — backtesting" row and the "Auth" row of the §6 fact table
(post-resolution CLAUDE.md line 8398):

3 rows ABOVE (unchanged, clipped for display):
```
| Options instrument type | **`CE` / `PE`** (Call / Put); `FUT` for futures | `groww/constants.py:95`; `groww/api-reference/03-backtesting.md:16-22`;  …⟨clipped for display — full line unchanged⟩
| Exchanges (in scope) | **NSE, BSE** (MCX excluded with the COMMODITY segment) | `groww/constants.py:92`; `groww/api-reference/04-annexures.md:39-43` …⟨clipped for display — full line unchanged⟩
| Rate limit — backtesting | **500 req/min, NO daily quota** (operator-confirmed 2026-06-13, NOT vendor-documented; DEC-GROWW-RATE-PERMIN-001). The pe …⟨clipped for display — full line unchanged⟩
```

RESOLVED ROW HERE (the full one-liner from the block above)

3 rows BELOW (unchanged, clipped for display):
```
| Auth | **TOTP flow** — `GrowwAPI.get_access_token(api_key, totp=pyotp.TOTP(secret).now())`; creds are **AWS-SSM-only** (DEC-AWS-ONLY-CREDS-001, MIGR …⟨clipped for display — full line unchanged⟩
| Instrument master URL | `https://growwapi-assets.groww.in/instruments/instrument.csv` (single CSV; SSRF-allowlisted host) | `groww/constants.py:24-2 …⟨clipped for display — full line unchanged⟩
| Instrument master schema | **21 columns** in fixed order; keys `exchange, exchange_token, trading_symbol, groww_symbol, name, instrument_type, segme …⟨clipped for display — full line unchanged⟩
```

## Mechanical application (recommended — no hand editing)

Run from the repo root while CLAUDE.md contains the conflict markers:

```bash
python3 - <<'PYEOF'
import io
p = 'CLAUDE.md'
s = io.open(p, encoding='utf-8').read()
start = s.index('<<<<<<< HEAD\n')
mid = s.index('=======\n', start)
end_marker_at = s.index('>>>>>>>', mid)
end = s.index('\n', end_marker_at) + 1
theirs = s[mid + len('=======\n'):end_marker_at]
resolved = theirs.rstrip('\n').replace(
    "`cli.py:5271-5272` + this file §2 rule 6",
    "`cli.py:5484-5485` (injected at the CLI live build site)"
    " + this file §2 rule 6") + '\n'
assert '(injected at the CLI live build site)' in resolved, 'insertion failed'
assert 'pull_controller.py:1026-1027' in resolved, 'post-P citations missing'
assert resolved.count('|') == 4 and resolved.startswith('| Adapter effective cap |'), 'row shape drifted'
io.open(p, 'w', encoding='utf-8').write(s[:start] + resolved + s[end:])
print('CLAUDE.md hunk resolved')
PYEOF
grep -c '<<<<<<<' CLAUDE.md && echo 'MARKERS REMAIN — STOP' || echo 'no markers left'
git add CLAUDE.md
```

If the assertions fail, the post-P side has drifted from this probe — STOP and
re-derive per the runbook fallback (do not improvise a hand-weave). After the
full refold is applied, `python3 scripts/ci/validate_citation_accuracy.py`
must print "✅ every extracted citation is accurate" — if it flags this row's
`cli.py` cite instead, repoint to the line numbers the scanner prints.
