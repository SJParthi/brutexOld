# Wave-D poison-corpus differential harness (ADR-0020 slice G9)

> DEC-LAKE-RUST-WAVED-HARNESS-001 · MIGRATION.md #1916 · 2026-07-16
> Harness: `tests/fakes/poison_corpus.py` (corpus) +
> `tests/integration/test_waved_poison_corpus_differential.py` (runner).

## 1. Purpose — the ADR-0020 Wave-D flip precondition

ADR-0020 (`docs/adr/0020-datalake-rust-conversion-roadmap.md`) gates the
Wave-D policy/orchestration flip (`lake_invariants` / `lake_heal`) on a
**poison-corpus differential**: the Rust twin may only take over after it
produces a verdict **equal** to the Python oracle's over every known poison
class. This harness IS that gate, shipped ahead of any Rust: a reusable
corpus of poisoned-lake builders (one `PoisonCase` per on-disk poison class)
plus a differential runner that drives TWO scanner implementations over
independent fresh builds of the SAME case and asserts normalized verdict
equality. **Today it runs Python-vs-Python only** (no Rust twin exists for
these modules yet) — the identity self-test doubles as a per-case
determinism pin (no clock / dict-order / absolute-path leak survives the
normalization). When the Wave-D crate lands, its scanner binding plugs into
the `ScannerImpl` seam and the SAME parametrized suite becomes the flip gate.

## 2. Corpus inventory (27 cases; provenance = the MIGRATION.md rounds that minted each class)

| # | case name | expected verdict | provenance |
|---|-----------|------------------|------------|
| 1 | `bars_intra_file_dup` | `IntraFileDup` | #1175 |
| 2 | `bars_foreign_month_pure` | `ForeignMonthRows` (overlap 0) | #1175 |
| 3 | `bars_foreign_month_overlap` | `ForeignMonthRows` (overlap counted) | #1175 |
| 4 | `legacy_key_drift_sibling` | `LegacyKeyDrift` | #1175 (#911 scope) |
| 5 | `legacy_key_drift_canonical_keys` | `LegacyKeyDrift` via `canonical_keys=` | #1175 |
| 6 | `bars_naive_timestamp_routing` | `ForeignMonthRows` (routed as-if-UTC) | #1594 |
| 7 | `bars_zero_row_file` | clean | #1594 r3 |
| 8 | `bars_schema_alien_missing_columns` | `SchemaAlienFile` + skip | #1510 → #1762 |
| 9 | `bars_price_sanity_rows` | `PriceSanityRows` | #1510 |
| 10 | `bars_out_of_range_datetime` | skip, sibling clean | #1594 r18 |
| 11 | `option_intra_file_dup` | `OptionIntraFileDup` | #1594 |
| 12 | `option_foreign_month_pure` | `OptionForeignMonthRows` (overlap 0) | #1594 |
| 13 | `option_foreign_month_overlap` | `OptionForeignMonthRows` (overlap 1) | #1594 |
| 14 | `option_multi_month_overlap` | overlap SUMMED across months | #1594 r1 |
| 15 | `option_null_side_foreign_key` | `OptionForeignMonthRows` (side = payload) | #1594 |
| 16 | `option_non_canonical_stem` | dup-only abstain (`2024-6`) | #1594 r2 |
| 17 | `option_unicode_digit_filename` | dup-only abstain (`²²-06`) | #1594 r2 |
| 18 | `glob_metachar_series` | literal-bytes reads only (`X[12]`) | #1594 r15 |
| 19 | `option_nested_dtype_list_side` | skip + sibling found | #1594 r12 |
| 20 | `option_nested_dtype_struct_side` | skip, no finding | #1594 r12 |
| 21 | `option_out_of_range_datetime` | skip + sibling found | #1594 r18 |
| 22 | `option_mass_poison_majority` | 11× `OptionForeignMonthRows` (heal-side >50% rail input) | #1594 r16 |
| 23 | `symlinked_series_dir` | outside poison unseen | #1594 r1 |
| 24 | `symlinked_options_root` | outside poison unseen | #1594 r2 |
| 25 | `symlinked_partition_file` | skip counted, sibling scanned | #1594 r4 |
| 26 | `symlink_loop_partition_file` | finding stands, sibling read abstains | #1594 r5 |
| 27 | `dangling_symlink_option_file` | scan survives, sibling found | #1594 r11 |

## 3. The differential contract (verdict normalization)

`scan_verdict(report, lake_root)` folds a `LakeScanReport` into the
comparable shape:

- **Included:** every counter (`files_scanned`, `files_skipped`,
  `series_scanned`, `option_files_scanned`, `option_files_skipped`,
  `files_by_segment`, `engine_files`, `engine_files_skipped`,
  `first_skipped_path`), findings as sorted `(type-name, field-dict)`
  tuples, and the census (`build_lake_census` → `census_to_dict`).
- **Excluded:** `duration_s` and every `*_utc` key (wall clock), and log
  TEXT (an implementation detail — event names/fields stay pinned by the
  unit suites, not by this harness).
- **Path folding:** every absolute lake-root prefix becomes `<LAKE>` so two
  lakes at different tmp roots compare equal (a Rust impl must emit paths
  under the same lake root it was handed).

`run_differential(impl_a, impl_b, case, tmp)` builds TWO INDEPENDENT fresh
lakes (a heal-capable impl may mutate its lake), runs one impl over each,
and returns both normalized verdicts for equality assertion.

## 4. Exception-taxonomy table (what the Rust twin must mirror)

Census (grep, 2026-07-16). The scan/heal surface is **catch-and-fold by
design** — raised types are rare; the load-bearing law is the CAUGHT sets.

| module | raised types | caught-and-folded sets | surface |
|--------|--------------|------------------------|---------|
| `timeframe_materialize.py` | `ValueError` ×2 · `NoStored1mBarsError` (ValueError subclass, the #1792 no-source arm) · re-raises | per-symbol quarantine machinery | quarantine / no-source class |
| `lake_heal.py` | `OptionWriterError` ×1 · `NotImplementedError` ×1 | `_PER_FINDING_HEAL_ERRORS` = `WriterError`, `OptionWriterError`, `PartitionLockTimeout`, `*parquet_read_skip_errors()`, `*datetime_out_of_range_escape_errors()`; `_SWEEP_GUARD_ERRORS` = `Exception` + the escape set (the L3 boot blanket) | per-finding isolated error; L3 never crashes boot |
| `catalog_reconcile.py` | `PartitionError` ×1 · `OverflowError` ×1 | per-file GAP-#9 skip | skip-and-count |
| `lake_invariants.py` | `TypeError` ×1 | `parquet_read_skip_errors()` + `datetime_out_of_range_errors()` at every read/classify site | skip-and-count (scanner never raises on poison) |

COMMON error-set helpers (`brutex/data/errors.py`) the Rust twin must
mirror 1:1: `parquet_read_skip_errors()` (:321),
`datetime_out_of_range_errors()` (:344),
`datetime_out_of_range_escape_errors()` (:383); plus `ResampleInputError`
(:32), `LakeWriteHalt` (:50) and `OptionWriterError`
(`option_parquet_writer.py:154`). Halt semantics (`LakeWriteHalt`
disk-full/OOM) must keep propagating past every per-finding isolation.

## 5. Out-of-corpus runtime classes + adjacent-scope rows

**Runtime-injection classes (NOT on-disk shapes — deliberately out of the
corpus, already pinned by `tests/unit/test_lake_heal.py`):** the mid-heal
swap / TOCTOU family (LH `test_option_nested_dtype_swap_mid_heal_*`,
monkeypatched merge-spy re-plants) and the `PanicException`-at-L3 class
(LH `test_run_sync_survives_panic_exception`). A Rust twin inherits these
obligations through the UNIT suites, not through this harness.

**Adjacent-scope rows (other modules own them; corpus optional):** FIFO /
non-regular inode + oversized 16 MiB shard + deep-JSON nesting bomb +
Unicode-digit rels (`lake_stats_manifest` — `tests/unit/test_lake_stats_manifest.py`)
and the dir-shaped `.parquet` (`greeks_backfill` —
`tests/unit/test_greeks_backfill.py`). They join the corpus only if/when
those modules enter a Rust wave.

## 6. Wave-D conversion order + go/no-go checklist

Conversion order (risk-ascending; `lake_invariants` LAST because every
earlier module's differential reuses its Python scan as the oracle):

1. `timeframe_materialize` → 2. `lake_heal` → 3. `catalog_reconcile` →
4. `lake_invariants` (last).

Go/no-go for ANY Wave-D flag flip (all must be green):

- [ ] The Rust impl passes `TestPoisonCorpusDifferential` bound as `impl_b`
      against the Python oracle (`impl_a`) over the FULL corpus.
- [ ] The heal-side phase-2 differential (below) is implemented and green
      before any `lake_heal` flip.
- [ ] The exception-taxonomy table (§4) is re-censused and the Rust twin's
      fold sets verified equivalent (skip / finding / isolated-error / halt).
- [ ] The unit suites (`test_lake_invariants.py`, `test_lake_heal.py`,
      census + schema-alien suites) pass UNMODIFIED with the flag ON.
- [ ] Byte-identity: flag OFF is byte-identical (never-routes spy), the
      #1531/#1532 burn-in-autorevert pattern wired (ADR-0020 PR-1 template).

**Phase 2 (documented, NOT shipped in G9):** the heal-side differential —
`heal_findings(..., mode=observe)` HealReport-summary equality + wet-mode
tree-state equality via a `relpath → sha256` map over both healed lakes
(the LH `_tree_state` idiom).

## 7. How to add a poison class

1. Add a builder `build_<name>(lake: Path) -> None` to
   `tests/fakes/poison_corpus.py` (raw-pyarrow plant for poison, the REAL
   writer for clean data; fixed 2024-06-anchored timestamps; no clocks).
2. Append a `PoisonCase` row to `CORPUS` with the exact
   `expected_finding_types` / `expects_skip` / `scan_kwargs` / `requires`.
3. Add the name to `_REGISTERED_IN_SCOPE_CLASSES` in the test module — the
   coverage test refuses a corpus that silently drops a registered class.
4. Record the class's provenance (MIGRATION.md entry) in the §2 table.

## 8. Honest limits (Golden Rule #10)

- The harness proves verdict parity **over the corpus**, not over all
  possible lake shapes — it complements, never replaces, the unit suites.
- **Python-vs-Python only today**: the identity self-test proves the
  harness + determinism, not any Rust parity (none exists yet).
- The scan-side differential only; heal-side (HealReport + tree-state
  bytes) is the named phase 2.
- Log text/event equality is deliberately out of the contract (owned by
  the unit suites' `capture_logs` pins).
- `expects_skip=False` asserts nothing about skip counters (some skip
  shapes — series-dir/root symlinks, dangling links — have deliberately
  unpinned counter semantics here; the finding-set equality is the tooth).

## 9. Completion note (2026-07-19 — DEC-RUST-LAKE-WAVED-001, MIGRATION.md #1954)

The readiness record this runbook exists for is SATISFIED: the Wave-D
conversion (`claude/rust-lake-waved-001`) bound the Rust scan as `impl_b`
at this harness's own `ScannerImpl` seam and drove ALL 27 corpus cases
differentially — 27/27 IDENTICAL and 27/27 ENGAGED (zero degrades) in both
the Python-vs-Rust and Rust-vs-Rust directions — BEFORE any verdict path
flipped, with the harness itself byte-UNMODIFIED (§1's law). The §6 go/no-go
checklist then completed through the endgame: default-ON, then the Python
scanner/heal cores DELETED in-lane (the coordinator/operator greenfield
ruling), so **the harness now drives the Rust path natively** — its
`test_expected_verdict` identity self-test is the standing frozen
finding-type/skip census over the crate scan, and the per-module
differentials pin Rust-vs-Rust determinism + frozen expectations in place
of the retired Python arm. The §4 exception-taxonomy table's classes carry
ruled per-class ISOLATED-error postures crate-side (per-finding heal
isolation · the GAP-#9 reconcile skip · the loud counted scan skip · the
typed read refusal); §5's phase-2 heal-side differential shipped as the
twin-lake four-strength heal gate (40/40). Adding a poison class per §7
still works unchanged — the runner and normalizer are seam-agnostic.
