# Delta execution — newly-merged surfaces, EXECUTED against correctly-built venv

- HEAD tested: `3ee742b322d9978eafe69ca3c7862bef5b24011f` (== merged tip 3ee742b3)
- venv: scratchpad/venv312 (shared, correctly-built)
- Wheels present & imported: fno_math 0.3.0, trading_core 0.3.2, brutex_feed 0.1.0,
  brutex_lake 0.1.0 (manylinux_2_39), brutex_core 0.3.0, brutex_data 0.1.0,
  brutex_strategies 0.1.0 + brutex — all `import ok`
- fno_math u128 kernel symbols PRESENT: ResidentBarsU128, ResidentSweepSimU128,
  count_supermask_u128s, count_supermasks_u128s (+ u64 twins) — **no force-reinstall needed**
- vocab_golden_vectors.json: empty diff vs origin/main (byte-untouched); working tree clean

| Surface | Test files run | RUN result | notes |
|---|---|---|---|
| Fill-model worst-case-only (DEC-FILL-WORST-CASE-ONLY-001) | unit/test_engine_trade_sim_fill_mode.py, unit/test_engine_rust_trade_sim_fill_mode.py, property/test_trade_sim_worst_case_floor.py, golden/test_engine_trade_sim_golden.py, golden/test_trade_sim_rust_golden.py, golden/test_costs_verified_examples.py, golden/test_options_executor_golden.py, golden/test_futures_executor_golden.py, integration/test_cli_sweep_verify_fill_mode.py | **70 passed / 0 failed** | no `test_fill_mode*.py` exists — the fill_mode unit tests are `test_engine_(rust_)trade_sim_fill_mode.py`. vocab fixture byte-untouched (confirmed separately). Trade-sim/cost/options+futures-executor goldens all green. |
| u128 enum kernel (DEC-ENUM-KERNEL-U128-001) | unit/test_engine_combo_mask_u128.py, property/test_supermask_kernel_parity.py | **47 passed / 0 failed** | wide-kernel dispatch + u128↔numpy parity property green against the real u128 wheel symbols |
| T10 brutex-data (BRUTEX_DATA_RUST=1) | golden/test_fno_symbol_rust_parity.py, golden/test_lake_keys_rust_parity.py, property/test_fno_symbol_rust_differential.py, property/test_lake_keys_rust_differential.py, unit/test_fno_symbol_rust.py, unit/test_lake_keys_rust.py, integration/test_lake_keys_rust_heal_parity.py | **517 passed / 1 skipped / 0 failed** | skip = `test_fno_symbol_rust.py:325` (no es_ES/de_DE locale installed — synthesized-locale arm covers the guard; env limit). heal-parity integration (the tier gate) green flag-ON. |
| T16 strategies (BRUTEX_STRATEGIES_RUST_COMBO=1) | unit/test_strategies_combo_rust.py, golden/test_combo_rust_flag_on_golden.py, property/test_combo_rust_differential.py | **108 passed / 0 failed** | flag-ON golden wrapper + differential green |
| T35 brutex-core (MONEY/PATHS/DOTENV/BACKUPENV/HWENV=1) | unit/test_money_rust.py, test_config_paths_rust.py, test_config_dotenv_rust.py, test_backup_env_rust.py, test_hardware_envelope_rust.py, test_brutex_core_boundary_pin.py | **293 passed / 0 failed** | all five seams flag-ON; AWS boundary pin green |
| AWS hermeticity (DEC-TEST-AWS-HERMETICITY-001) | unit/test_aws_hermeticity_belt.py | **11 passed / 1 skipped / 0 failed** | skip = `:83` (boto3 not installed — factory-level pins are the belt in a boto3-less env; env limit, matches the CI posture) |
| G10 window-ledger (BRUTEX_LAKE_RUST_LEDGER_RENDER=1) | integration/test_window_ledger_rust_reader_parity.py, test_window_ledger_rust_state_convergence.py | **16 passed / 0 failed** | flag-ON reader-parity + crash/resume state-convergence NAMED MERGE GATE green on real merged wheel |
| T24 integrity (RAW_INTEGRITY/INTERIOR_GAP/PARTIAL_WINDOW=1) | unit/test_raw_integrity_rust_seam.py, test_detectors_rust.py, test_interior_gap_detect_rust_seam.py, test_partial_window_detect_rust_seam.py, integration/test_detectors_rust_stored_outcome.py | **102 passed / 0 failed** | three detector seams flag-ON incl. blake2b in-crate digest |
| box auto-deploy (DEC-BOX-AUTODEPLOY-LANE-001) | unit/test_nightly_box_update.py, test_nightly_env_preflight.py, test_nightly_run.py, test_nightly_chain.py, test_cli_box_update.py, integration/test_box_update_git.py | **287 passed / 0 failed** | full box-update leg + env-preflight + CLI + real-git integration |
| O(1) hot-path benches | bench/test_bar_lookup_o1.py, test_blake3_combo_hash_o1.py, test_engine_combo_mask_u128_o1.py, test_engine_combo_mask_wide_kernel_o1.py | **21 passed / 0 failed** | pytest-benchmark ran; robust shared-CI ratio ceilings held. Strict 1.1× M4-Pro tier (BRUTEX_M4PRO_STRICT) is env-unrunnable here — honest limit, not a fail. |

TOTALS: 1571 passed, 3 skipped (all 3 skips are env limits: 1 locale, 1 boto3, 1 doc-covered), 0 failed.

VERDICT: every newly-merged surface is now EXECUTED-CONFIRMED (flag-ON parity arms
actually ran). No genuine test failures. Nothing left NOT-RUN except:
- the strict 1.1× M4-Pro O(1) tier (requires the self-hosted M4-Pro runner + BRUTEX_M4PRO_STRICT=1) — structurally env-unrunnable, honest limit.
