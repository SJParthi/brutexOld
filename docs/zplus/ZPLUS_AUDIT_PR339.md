# Z+ audit — PR #339 (DEC-PREOPEN-AMPERSAND-001 + DEC-PREOPEN-XLSX-STYLE-001)

> Per `operator-charter-forever.md §C/§E` + DEC-P0-014 (`docs/zplus/ZPLUS_PR_DISCIPLINE.md`):
> the 4-agent hostile review + Z+ matrices for PR #339, recorded as the
> per-PR audit artifact (mirrors `ZPLUS_AUDIT_PR33_34_35.md`).
> Reviewed sha range `d67fedb..d1e9184`. One post-implementation pass
> (the change is `brutex/dashboard/`-only, < 1000 LoC — the discipline
> doc's "one pass acceptable" tier). Run 2026-06-03.

## Change under review

1. **DEC-PREOPEN-AMPERSAND-001** — `brutex/dashboard/db_view.py` `_SYMBOL_RE`
   gains `&` (`^[A-Za-z0-9][A-Za-z0-9_.&\-]*$`). Root cause: the read-side
   allowlist disagreed with the write-side denylist (`brutex/data/partition.py`
   `_FORBIDDEN_SYMBOL_CHARS`, which permits `&`), so the pre-open report 500'd
   on `M&M`.
2. **DEC-PREOPEN-XLSX-STYLE-001** — `brutex/dashboard/preopen_report.py` OOXML
   writer gains a bold/centred merged title row, a bold header + bold SYMBOL
   column, precise `<cols>` widths, and `<mergeCells>`; `app.py` passes the
   date-range title.

## 4-agent hostile review — findings

| Agent | Lens | CRITICAL | HIGH | MEDIUM | LOW | Verdict |
|-------|------|:--:|:--:|:--:|:--:|---------|
| 1 | Hot-path / perf / O(1) | 0 | 0 | 0 | 0 | Ship |
| 2 | Security (traversal / glob / injection / supply-chain) | 0 | 0 | 0 | 1 | Approve |
| 3 | Correctness / determinism / OOXML validity | 0 | 0 | 0 | 2 | Approve |
| 4 | Cite/path verification | — | — | — | — | 20/20 VERIFIED |

**Agent 1 (hot-path):** `_SYMBOL_RE` is a single non-overlapping char-class
star with a distinct mandatory first char — no ambiguity, no nested
quantifiers → no ReDoS (empirically linear: 500k chars in ~3.7 ms). Per-row
`_sheet_xml` build is O(1)/row (list-append + one terminal `join`, no `+=`
accumulation); `header.index("SYMBOL")` is hoisted out of the loop. This is the
offline report path, NOT the engine's O(1)-per-bar contract (Golden Rule #10).

**Agent 2 (security) — LOW-1:** leading/standalone `&` (e.g. `a&`) is accepted,
cosmetically broader than the three real tickers; harmless because `&` is inert
for both filesystem paths and DuckDB globs. Confirmed: `..`, `/`, `\`, `* ? [ ] { }`,
and space all still reject; `read_parquet(?)` is genuinely parameter-bound (no
injection); `openpyxl` is test-only (no runtime dep added); `noqa`/`nosec` count
unchanged (12 → 12).

**Agent 3 (correctness) — LOW-2:** two latent, test-guarded couplings —
`header.index("SYMBOL")` would raise if SYMBOL ever left `REPORT_COLUMNS`, and
`_cols_xml` indexes `_COL_WIDTHS[i]` (guarded by `test_col_widths_aligned_to_columns`).
Confirmed: CT_Worksheet child order valid (`sheetViews→cols→sheetData→autoFilter→mergeCells`);
openpyxl opens both empty + data workbooks with no repair; byte-identical
re-render (determinism); `..` rejected by a separate `".." in symbol` guard
independent of the regex.

**Agent 4 (cite/path):** all 20 cited paths/symbols/lines/decision-IDs/fixture-lines
VERIFIED — `M&M` at `tests/fixtures/fo_mktlots_real.csv:85`, `GVT&D` at `:73`,
`M&MFIN` confirmed **absent** from that fixture (matches the corrected ledger claim).

## Disposition

No CRITICAL/HIGH/MEDIUM. The 3 LOW items are latent couplings already pinned by
tests — no code change required. Ship.
