# G10 — `window_ledger_store` byte-parity SPIKE: escalation to the coordinator

> **ADR-0020 slice G10** (`docs/adr/0020-datalake-rust-conversion-roadmap.md:191`
> — the LAST-MILE named escalation point).
> **DEC-LAKE-RUST-LEDGERSTORE-SPIKE-001** · MIGRATION.md #1917 — allocator-final.
> Spike branch: `claude/rust-lake-ledgerstore-spike` (origin/main @ bd3a20146).
> **ZERO production-code change** — `brutex/data/window_ledger_store.py` is
> byte-untouched; this directory carries the measured evidence + the priced
> options for the coordinator's ruling.

## 1. VERDICT — byte-identity NOT FOUND (F3 confirmed by measurement)

The polars-native (Rust) encoder **cannot reproduce the current pyarrow-25
ledger bytes under ANY exposed writer-option tuple** — confirming ADR-0020's
F3 "infeasible by construction" finding
(`docs/adr/0020-datalake-rust-conversion-roadmap.md:260`) with executed
evidence instead of reasoning:

- **Corpus:** 7 cases (a)–(g) built through the PRODUCTION writer
  (`save_window_ledger` / `append_ledger_delta`,
  `brutex/data/window_ledger_store.py:795` delta + `:927` base — both
  `pq.write_table(table, tmp)` all-defaults): empty ledger · 1-row · mixed
  record+plan (None arms) · None-optional-column rows via the
  `_rows_to_table` seam · unicode/long `last_error` · ~10k rows · one delta
  file. All timestamps seeded from the frozen `_NOW`.
- **Sweep:** per case, TWO DataFrame construction twins
  (`pl.from_arrow(_rows_to_table(rows))` + an explicit
  `pl.String`/`pl.Int64`-schema `pl.DataFrame`) × 7 option tuples
  (`compression="snappy"` — polars defaults zstd; ± `statistics`
  True/False/"full"; ± `row_group_size` matched to the pyarrow footer; ±
  `data_page_size=1 MiB`; plus the polars-default control), all
  `use_pyarrow=False` — the ONLY arm that answers the Rust feasibility
  question. **14 arms × 7 cases: ZERO sha256 matches.**
- **Environment:** pyarrow 25.0.0 · polars 1.42.1 · CPython 3.12 (Linux);
  the pyarrow ground truth was introspected from the LIVE library
  (`inspect.signature(pq.write_table)` — the full defaults dump is in the
  transcript), never from memory.
- **Determinism sanity:** the pyarrow production write IS byte-stable across
  two runs of the same ledger (`pyarrow_write_deterministic_across_runs:
  true`) — the divergence below is encoder identity, not nondeterminism.
- **Bridging control** (`use_pyarrow=True`, documenting only): even the
  pyarrow re-entry arm is NOT byte-identical (polars passes its own defaults
  into pyarrow). Labelled VACUOUS for the Rust route either way
  (pyarrow==pyarrow proves nothing about a Rust producer).

### First-divergence evidence (verbatim from the committed transcript)

| case | rows | pyarrow sha256 (12) | best polars sha256 (12) | first divergence | sizes (pa/pl) |
|---|---|---|---|---|---|
| a_empty | 0 | `a4dfdbd112fd` | `087199d20650` | offset **5** (`0x04`→`0x02`) | 2724 / 1692 |
| b_one_row | 1 | `963520fb3499` | `40d016441627` | offset **15** (`0x12`→`0x00`) | 4581 / 5718 |
| c_mixed_plan | 3 | `83486772093c` | `4d06d0b03869` | offset **15** (`0x12`→`0x00`) | — |
| d_none_optional | 2 | `a04409c46743` | `11158fda89d4` | offset **15** (`0x12`→`0x00`) | — |
| e_unicode_error | 1 | `003f7ebd529a` | `bf05614226ce` | offset **15** (`0x12`→`0x00`) | — |
| f_10k_rows | 10000 | `a9c2fa800431` | `a0ccab1e7ad7` | offset **15** (`0x12`→`0x00`) | 71329 / 52003 |
| g_delta | 1 | `640b9edfa1a3` | `9129013776c1` | offset **15** (`0x12`→`0x00`) | 4601 / 5754 |

Best arm everywhere: `from_arrow/snappy_defaults` (option knobs beyond
compression never moved the first-divergence offset). Full per-arm records:
`probe_transcript.json` (this directory).

### Divergence classes (why NO option tuple can close the gap)

1. **First DATA PAGE header thrift** — divergence begins at byte ~15, inside
   the very first page header (pyarrow data-page-v1 layout + dictionary
   strategy vs polars' page layout). Not an exposed knob on either side.
2. **`created_by` footer string** — pyarrow `parquet-cpp-arrow version
   25.0.0` vs polars `Polars`. Polars' `DataFrame.write_parquet` exposes
   **no `created_by` knob** (signature captured in the transcript).
3. **Footer `format_version` marker** — pyarrow writes `2.6`, polars `1.0`.
   No polars knob.
4. **`ARROW:schema` key-value flatbuffer BYTES** — BOTH encoders embed the
   key (pyarrow via its `store_schema=True` default; polars writes its own),
   but the serialized flatbuffer is NOT identical (measured case (c): 1248 vs
   1244 bytes) and neither side exposes a byte-level knob for it. (Measured
   correction over the initial sweep's classifier, recorded in the
   transcript's `measured_correction` field.)
5. **Empty-file structure** — pyarrow renders 1 empty row group; polars 0
   (case (a) diverges from byte 5, straight into the footer thrift).

Classes 2–4 are *structural encoder identity*: closing them requires either
patching polars/pyarrow or exact-pinning the producing library — exactly the
priced escalation ADR-0020 §3 anticipated.

## 2. THE ADR LAW (verbatim — ADR-0020 §3, `docs/adr/0020-datalake-rust-conversion-roadmap.md:191-212`)

> 1. **Attempt the polars-native route first** — migrate the store's render
>    onto the polars encoder family (both sides through the same crate-gated
>    seam), bringing it under the SAME crate↔wheel lockstep parity apparatus as
>    PR-1/PR-2. Note honestly: this changes the on-disk bytes going forward
>    relative to today's pyarrow output (the module's own contract is the
>    row-level fixed point, F3 — the byte-identity gate then holds Rust-path vs
>    the NEW polars-rendered Python path, not vs historical pyarrow bytes).
> 2. **If bit-identity is infeasible without an exact-pin policy change**, this
>    ONE module **escalates to the coordinator with the priced exact-pin DEC**
>    (`DEC-LAKE-EXACT-PIN-001`: exact-pin Python polars + a lockfile + vendored
>    polars at the matching `py-*` git tag + transitive-zstd pin + wheel-build
>    byte-probe gate; costs per the maximalist pricing — ~5–15 min + hundreds
>    of transitive deps per wheel build, a permanent two-sided upgrade tax, a
>    standing single-byte-FAIL exposure). This is the named escalation point;
>    it is not softened here.

The spike executed step 1's feasibility probe; the measured answer is
"infeasible vs the CURRENT pyarrow bytes", which lands us at step 2 — this
escalation. Nothing here is softened.

## 3. THE THREE PRICED OPTIONS (coordinator to rule; none is adopted here)

### (i) DEC-LAKE-EXACT-PIN-001 — exact-pin the producing library

Exact-pin Python polars + a lockfile + vendored polars at the matching
`py-*` git tag + transitive-zstd pin + wheel-build byte-probe gate (the
ADR-0020 §3 text above, quoted verbatim). **Costs (per the maximalist
pricing recorded in the ADR):** ~5–15 min + hundreds of transitive deps per
wheel build; a permanent two-sided upgrade tax (crate rev == installed wheel,
forever); a standing single-byte-FAIL exposure. **Spike note:** even this
buys parity only against the NEW polars-rendered bytes — nothing can make a
Rust producer emit today's *pyarrow* bytes (classes 2–4 are pyarrow encoder
identity).

### (ii) ROW-EQUIVALENCE LAW — ratify the status quo

Keep `pq.write_table` at both sites
(`brutex/data/window_ledger_store.py:795,927`); gate any future Rust port on
the `to_rows()` fixed point + footer-SEMANTIC equivalence, never bytes. This
is what the module's own docstring already contracts
(`brutex/data/window_ledger_store.py:16` — "Parquet *file bytes* may differ
across writers via embedded library metadata; the ROW CONTENT is the
reproducible contract"). The spike's committed probe test
(`tests/golden/test_window_ledger_bytes_probe.py`) proves the row-level fixed
point HOLDS across encoders: the polars-written file read back through the
production `_read_ledger_file_rows` yields row tuples IDENTICAL to the
pyarrow file's. **Cost (disclosed, not hidden):** one class of gate weaker
than PR-1's byte gate for this ONE module.

### (iii) GOLDEN RE-BASELINE — ADR route-1, adopt polars-native bytes forward

Move both `_write_parquet` closures onto a polars-native render seam
(default-OFF flag per the PR-1 `BRUTEX_LAKE_RUST_RENDER` pattern), adopt the
NEW bytes going forward under the row-level contract, and hold the future
Rust byte gate against the NEW polars-rendered Python path. **Costs:** a
historical-vs-new byte discontinuity (loaders unaffected — the reader is
schema-name-based per `_read_ledger_file_rows`, and the spike's probe test
proves the cross-encoder read); a one-time golden re-lock; the PR-1 lockstep
tax inherited (crate rev == installed wheel + the version guard).

## 4. WHAT IS COMMITTED (this spike, branch B of the decision tree)

| artifact | path |
|---|---|
| Probe transcript (per-case: shas, option tuples, first-divergence offsets, footer diffs, divergence classes; defaults dump; env) | `docs/lake/g10_window_ledger_byte_parity/probe_transcript.json` |
| Always-green probe test pinning the DOCUMENTED divergence class + the cross-encoder row fixed point (CI pins the finding without flaking) | `tests/golden/test_window_ledger_bytes_probe.py` |
| This escalation | `docs/lake/g10_window_ledger_byte_parity/ESCALATION.md` |

Production code: **zero behavior change** (nothing under `brutex/` touched).
The probe driver itself was a scratchpad script (uncommitted by design —
handoff §6); the committed test re-derives the load-bearing corpus case
through the production writer on every CI run.
