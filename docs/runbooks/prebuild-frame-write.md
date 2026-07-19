# Runbook — the FRAME-DIRECT derived-TF prebuild write (DEC-PREBUILD-FRAME-WRITE-001)

> The verify-or-degrade frame-direct fast lane for the pipeline prebuild's
> derived-timeframe writes. **PR-1 ships DARK (default OFF)** — at defaults the
> prebuild runs the verbatim legacy path, byte-identical by construction
> (negative-pinned). This runbook covers the kill switch, the operator SHADOW
> run (the PR-2 default-ON evidence generator), the degrade taxonomy, and
> autorevert triage.

## 1. The kill switch — `BRUTEX_PREBUILD_FRAME_WRITE`

Resolved once per derive builder (`make_lake_write_tf`) + per write
(`ParquetWriter.write_frame` G0); the vocabulary rides the COMMON
`brutex.config.env_flags.env_flag_tristate` plus one extra literal:

| value | behavior |
|---|---|
| unset / blank | the default constant (`PREBUILD_FRAME_WRITE_DEFAULT` — **`False` in PR-1**; the PR-2 flip is that ONE constant) |
| `1` / `true` / `yes` / `on` | the fast lane + the per-process burn-in (below) |
| `0` / `false` / `no` / `off` | legacy, silent — the explicit operator kill switch (honoured regardless of the default) |
| `shadow` | BOTH lanes on every eligible write; the LEGACY bytes ship; divergences are counted + ERROR-logged (zero byte risk) |
| anything else | **fail-safe OFF + one warn-once `flag_unrecognised`** — the rust_metrics kill-must-kill rule: under a default-ON flip a typo'd kill switch (`0ff`, `flase`) must still KILL, never crash the prebuild. A sanctioned, DEC-recorded deviation from the house fail-loud-env law. |

Revert levers, weakest to strongest: the autorevert latch (automatic,
per-process, zero operator action) → `BRUTEX_PREBUILD_FRAME_WRITE=0` per
process → the `PREBUILD_FRAME_WRITE_DEFAULT` constant (one-line PR). Derived
data is recomputable: after ANY doubt, kill the flag and re-derive
(`incremental=False` restates every partition latest-wins).

## 2. The operator SHADOW run (the PR-2 evidence step)

One full prebuild over the REAL ~750-symbol lake with the flag at `shadow` —
the ONLY place pre-DEC off-grid legacy months exist at scale. Zero byte risk:
every write ships the LEGACY bytes; the fast lane is computed beside it and
bit-compared. **Manual env + the ordinary prebuild — deliberately ZERO new CLI
surface** (coordinator ruling):

```bash
# On the operator box, before launching the dashboard / nightly whose
# prebuild phase should run shadow:
export BRUTEX_PREBUILD_FRAME_WRITE=shadow
# then run the ordinary pipeline prebuild (the dashboard ▶ pull's final
# phase, or the nightly's PREBUILD leg — no new command).
```

Expected artifact: the per-symbol `lake.derived.write_summary` INFO lines
carry the additive counters — over the whole run expect
`frame_shadow_checked` in the ~10⁵–10⁶ region (every eligible derived write)
and **`frame_shadow_mismatches == 0`**. Any mismatch is ALSO a never-throttled
`prebuild.frame_write.shadow_mismatch` ERROR naming the partition, the lane
and the diverged `WriteResult` fields. A clean transcript
(`frame_shadow_mismatches == 0` summed across symbols) is the licensing
evidence for the PR-2 default-ON flip; the transcript is brokered by the
coordinator after PR-1 merges. Cost: every eligible write pays legacy + fast
(combined cost unmeasured — disclosed; run it on an idle evening).

## 3. Burn-in + autorevert (ON mode)

Per process, the first 8 FRESH and 8 MERGE fast-lane admissions ALSO run the
legacy computation, ship the LEGACY bytes, and bit-compare the two
`WriteResult`s (fresh: including the rendered-bytes sha256). A mismatch:

* latches the process OFF (`prebuild.frame_write.autoreverted` ERROR — never
  throttled — carrying partition, lane, the diverged fields and both
  sha256s); every later write short-circuits to legacy (warn-once
  `prebuild.frame_write.degraded reason=autoreverted`);
* has written NO fast-lane byte (legacy shipped during burn-in).

Triage an autorevert: (1) capture the ERROR line's partition + diverged
fields; (2) confirm the lake state (`brutex lake audit-unique`; the partition
named); (3) re-run the A/B corpus locally
(`pytest tests/integration/test_prebuild_frame_write_ab.py`) — a polars/env
drift shows up there; (4) leave the flag OFF (or rely on the latch) and file
the finding with the transcript. The budgets re-arm at every process start —
each dashboard boot / nightly / crossverify leg re-verifies the environment.

## 4. Degrade taxonomy (observability ONLY — a degrade never decides correctness)

Every unprovable shape hands the partition to the VERBATIM legacy path, which
owns the bytes AND the errors. Reasons (`prebuild.frame_write.degraded`):

| reason | class | meaning |
|---|---|---|
| `disabled` / `autoreverted` / `flag_unrecognised` | gate verdict (not counted) | the call took legacy by GATE decision |
| `merge_delta` | **NORMAL** (DEBUG + counter) | a genuine add/restate — the ~20 real current-month rewrites per symbol per daily pull; the legacy union runs |
| `schema_mismatch` | anomaly (warn-once) | frame shape/dtypes outside the resampler contract |
| `dup_or_unsorted_ts` | anomaly | duplicate/unsorted timestamps — legacy reproduces the dedup + `lake.vendor_duplicate_rows` WARN |
| `domain_violation` | anomaly | negative volume/OI, null/non-finite price, broken OHLC ordering — legacy raises TODAY's loud `CandleBar` error → per-symbol quarantine |
| `magnitude_overflow` / `snap_overflow` | anomaly | a price at/above 1e12 (G4) / a snap-select overflow (G5 — unreachable behind G4) |
| `merge_stored_snap` / `merge_stored_dups` / `merge_overlay` / `merge_stored_schema` | anomaly | the stored partition is outside the provable envelope (snap-overflow class / pending dedup-heal / hand-enriched overlay / exotic schema) — legacy `_merge_write` handles heal/overlay/errors verbatim |
| `fast_lane_error` | anomaly | the G9 belt: ANY unexpected fast-lane exception before commit (`LakeWriteHalt` / `MemoryError` are NEVER caught — they propagate) |
| `shadow_mismatch` | ERROR, never throttled | a shadow divergence (non-latching — shadow enumerates) |

On a healthy lake every anomaly reason should be ~zero — each firing is
signal. Counters surface per symbol on `lake.derived.write_summary`
(`frame_fast` / `frame_degraded` / `frame_degrade_reasons` /
`frame_shadow_checked` / `frame_shadow_mismatches`; emitted only while the
gate is engaged — the flag-OFF summary is byte-identical to pre-change).

## 5. What the lane does NOT change

The monthly `<interval>/<yyyy>/<mm>.parquet` layout, the derive→partition
lock order + flock discipline, atomic tmp+fsync+replace per rendered file,
the stats-manifest + dataset-manifest hooks (keyed on
`WriteResult.file_rewritten`), the daily (`1day`) writers (legacy by design),
the vendor `write()`/`append()` entries (byte-untouched), run identity /
`data_snapshot_hash` (bytes identical ⇒ unmoved). fsync, parquet encode and
the resample math are the honest unaddressable floor. NO speedup number is
claimed until the operator's post-fix measurement (sandbox ratios recorded in
MIGRATION.md; a TINY symbol can measure SLOWER flag-ON — polars per-call
fixed overhead; the crossover measured ≈ a few thousand bars per symbol).
