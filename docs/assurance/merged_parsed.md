# 12-Lane Adversarial-Assurance Parse — bruteX merged tip `3ee742b3`

Journal: `wf_b20607e5-5d2/journal.jsonl` (66 lines: 28 result + 38 started).
28 results = 16 hunt (4 lanes re-run — identical task `key`) + 9 refute + 1 map + 2 critic.
**No top-level `confirmed_defects[]` exists in this journal** (grep empty); the confirmation verdict is derived from the 9 refuters (all `refuted:true`).

---

## A) 12-Lane Table

| Lane | Perms cov/total | Status | Evidence 1-liner (executed) | cand-defects |
|------|-----------------|--------|------------------------------|--------------|
| hunt:concurrency | 14/15 | REAL | `pytest test_partition_lock.py …` OFD-exclusion/deadline/registry-refcount + write-deadline lock-order (~446 tests) | 0 |
| hunt:resource | 13/15 | REAL | inline: `atomic_write_via` ENOSPC/EDQUOT/MemoryError/EACCES→LakeWriteHalt mapping + atomic_write_simple raw | 0 |
| hunt:corrupt | 9/15 | REAL* | `probe_dt.py` 2**62µs Datetime pyo3-panic containment at spot-derive read boundary (*critic: JSON truncated/incomplete) | 0 |
| hunt:vendor | 9/9 | REAL | `python -c` errors._CODE_TO_ERROR 429/5xx/504/408/empty-body retryable invariants | 0 |
| hunt:crash | 18/18 | REAL | probe: pull1 records 3 windows, durable-ledger crash-resume fold Python+Rust render byte-parity | 0 |
| hunt:correctness | 17/17 | REAL | money differential: 18 hostile boundary values `str(round_money)` HALF_UP flip-exact | 0 |
| hunt:o1 | 29/30 (+22/24 rerun) | REAL* | `pytest test_o1_tiers.py -q → 11 passed`; ceilings tighten under STRICT, never weakened (*tests/bench/ hot-path p99 UNDRIVEN) | 0 |
| hunt:sec-secrets | 5/6 | REAL | `git grep -nIE AKIA…/BEGIN PRIVATE KEY/totp_secret` P1 secret-literal sweep clean | 0 |
| hunt:sec-injection | 15/17 (+11/14 rerun) | REAL | `pytest` lake_heal.partition_error repr token/error fails-before/pass-after; option-tree escaping guards | 2 (both REFUTED) |
| hunt:sec-ssrf | 8/10 (+9/10 rerun) | REAL | `pytest test_groww_instrument…/nse_constituents/bhavcopy` http/file/non-allowlist/env-override refusals | 1 (REFUTED) |
| hunt:sec-ci | 14/14 | REAL | `pytest test_redteam_fork_pr_harden.py …` + premerge + automerge redteam suites | 0 |
| hunt:delta | 26/27 (+18/19 rerun) | REAL* | env-refusal SET worst_case/precise/'1' refuse executed; canonical_trade_params fill_mode==worst_case (*fill goldens + u128 kernel CODE-READ, stale wheel) | 0 |

`*` = self-reported REAL but rests partly on code-read / unrunnable gates (see D).

---

## B) DEFECT LEDGER

**3 distinct candidate defects across 12 lanes → 0 CONFIRMED, 3 REFUTED, 0 UNVERIFIED.**
Each got a 3-refuter panel; every refuter `refuted:true` `executed:true` → unanimous refute (3/3).

### CONFIRMED (survived majority): **NONE**

### REFUTED (3/3 panel each)
| # | Dim/Lane | Sev | Title | File | Panel | Why refuted |
|---|----------|-----|-------|------|-------|-------------|
| 1 | SSRF | MINOR/NIT | Outbound SSRF guards validate only initial URL — 302 from allowlisted host to internal target followed unchecked | `groww/instrument_loader.py`, `data/bhavcopy.py`, `dashboard/nse_constituents.py` (`_check_url`) | 3/3 refuted (mechanism reproduced via local 302→/internal server, body=`INTERNAL-SECRET-BODY`) | absence of defense-in-depth per-hop re-check, self-admitted NIT, not an exploitable vuln |
| 2 | log-injection | MINOR | BARS-tree `lake.invariant.scan_skip` forges log lines via control-char vendor symbol; `_FORBIDDEN_SYMBOL_CHARS` omits NL/CR/ESC/BEL | `data/lake_invariants.py:1349` (+1397/843/858), `data/partition.py:110` | 3/3 refuted (repro: `scan_lake_invariants` over `bars/NSE/CASH/'EVIL\nlake.invariant.all_clean forged\x1bM'` emitted raw NL+ESC at :1349) | mechanically real but judged non-actionable — vendor symbol cannot carry control chars in practice / pre-existing not newly-merged |
| 3 | log-injection | NIT | `partition._validate_symbol` accepts `'..'` → in-lake partition-path traversal | `data/partition.py:140`/`:166` | 3/3 refuted (repro: `_validate_symbol('..')` ACCEPTs; `build_partition_path(symbol='..')` resolves `…/bars/NSE/1minute/2024/01.parquet`) | `_FORBIDDEN_SYMBOL_CHARS` lacks `.`; judged non-actionable (in-lake only, no external symbol source is `..`) |

### UNVERIFIED: **NONE** (every candidate got a full 3-refuter panel).

No CONFIRMED defect → no DEC-fix owner required. If hardening desired anyway: (1)+(2)+(3) all sit in the lake/adapter data layer → owner `brutex/data` + `brutex/adapters/groww` (per-hop redirect re-check; extend `_FORBIDDEN_SYMBOL_CHARS` with `\n\r\x1b\x07` + `.`).

---

## C) Critic Output (2 critics, both agree)

- **critic.missing** (dominant, both critics): **ENV-BLOCKER — the `.venv-hunt` `fno_math` wheel is STALE vs this tip** (`.so` mtime 06:23 UTC predates the #1633/#1942 u128-kernel src commit 07:14 UTC; missing `ResidentBarsU128`/`count_supermask_u128s`/`count_supermasks_u128s`). `conftest.py`'s import chain pulls `fno_math`, so **every fno_math-touching collection errors at import**. Crates `brutex_core`/`brutex_data`/`brutex_strategies` are **NOT built** (`find_spec` False). Consequently UNEXECUTED: FILL-MODEL goldens (#1825), u128 enum kernel + `test_engine_combo_mask_wide_kernel_o1.py`, T10/T16/T35 flag-ON engaged parity/burn-in/autorevert, AWS-hermeticity belt (#1954), entire `tests/bench/` (O(1) p99 unmeasured), entire `tests/redteam/` incl. #1631 gitleaks diff-bound + bandit, G10 byte-parity spike probe.
- **weakest_lanes**: Engine/fill-model/u128-combo kernel (zero execution, highest-delta surface) · T35 brutex-core / T10 brutex-data / T16 brutex-strategies ON-arm parity (unbuilt crates) · AWS-hermeticity (collection-blocked) · O(1)-latency (bench undriven) · Security/CI-gate (redteam undriven) · Corrupt/hostile-inputs (truncated/incomplete).
- **recommend_more_rounds**: **true** (both critics).
- **notes**: "A round that cannot import the code under test is not a dry round — it is an un-run round." Two consecutive DRY rounds NOT reached; only Concurrency + Resource are genuinely clean (they exercise wheel-independent leaves + the two current crates brutex_lake/brutex_feed). "Absence-of-defect over surfaces never imported is not evidence of correctness." MANDATORY next: `for c in fno-math brutex-core brutex-data brutex-strategies; do (cd rust/$c && maturin develop --release); done` then drive the fill/u128/T10/T16/T35/AWS/O(1)/redteam lanes before any dry-round claim.

---

## D) EXECUTION HONESTY per lane

Every hunt lane self-labels **REAL** — but the two critics prove several REALs rest on **code-read only** because of the stale/unbuilt wheels.

**Genuinely EXECUTED (command output, wheel-independent or current crate):**
- concurrency (~446 tests, partition_lock/atomic_io/stat_deadline/summary_cache/greeks pool)
- resource (ENOSPC/EDQUOT/EACCES mapping inline)
- vendor (errors._CODE_TO_ERROR `python -c`)
- crash (durable-ledger byte-parity probe; brutex_lake ledger_render 104-pass, lake_rust_locks 75-pass are current-crate confirmations)
- correctness (18-value money HALF_UP differential)
- sec-secrets (`git grep` sweep)
- sec-injection (lake_heal repr fails-before/pass-after)
- sec-ssrf (unit gates + refuter local-302 repro)
- sec-ci (redteam fork_pr/premerge/automerge suites)
- o1 (`test_o1_tiers.py` 11 passed — *but only the ceiling machinery, not any hot-path bench*)

**REAL that rests on UNRUNNABLE / stale-wheel gates (flag):**
- **hunt:delta** — env-refusal + `canonical_trade_params` executed, but the **fill-model worst-case goldens, u128 enum kernel, T10/T16/T35 flag-ON engaged parity are CODE-READ** (fno_math stale; brutex_core/data/strategies unbuilt). Highest-delta surface, least-proven.
- **hunt:corrupt** — pyo3-panic-containment headline needs the built engine to actually raise; JSON truncated → treat INCOMPLETE.
- **hunt:o1** — `tests/bench/` (the actual p99 ratio) UNDRIVEN; only the tier-math ran.
- **MAP contradiction**: map `gate_proof` claims `import … all 8 → OK all 8` with `gates_ok:true`, but both critics found `find_spec` False for 3 crates + stale fno_math in `.venv-hunt`. The map's "gates proven" reflects a build state the hunt lanes did not share.

---

## E) MAP key facts
- Tip `3ee742b322d9978eafe69ca3c7862bef5b24011f` CONFIRMED reached; **7 crates**: fno-math 0.3.0, trading-core 0.3.2, brutex-feed 0.1.0, brutex-lake 0.1.0, brutex-core 0.3.0, brutex-data 0.1.0, brutex-strategies 0.1.0; 3 delta-DECs.
- Map `gates_ok:true` (import smoke "OK all 8" + unit) — **but disputed by both critics** (stale fno_math + 3 unbuilt crates in the hunt venv); take with the D caveat.
