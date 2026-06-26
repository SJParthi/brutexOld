# BruteX System Verification Report

> **Headline: 6 of 8 subsystems PROVEN, 2 PARTIAL, 0 GAP.**
> Adversarial re-check (attempted-refutation) verdicts. Every "proven" below is
> backed by a real captured result independently reproduced from a worktree; the
> two PARTIAL rows are stated honestly as partial and are **not** upgraded to
> proven (Golden Rule #10). This document is the evidence base for customer / EAP
> claims — do not cite a row beyond its Final status.

_Generated: 2026-06-26. Methodology: each subsystem was handed to an independent
verifier instructed to REFUTE the prior "proven" claim; the verdict recorded is
what survived the refutation attempt._

---

## 1. Verdict table

| Subsystem | Final status | What's proven / what's NOT | Evidence pointer |
|-----------|--------------|----------------------------|------------------|
| raw-integrity | **PROVEN** | Refutation FAILED. The 3 cited test files re-ran → **49 passed**. The 3 NEW modules (`raw_integrity.py`, `raw_integrity_store.py`, `integrity_verify.py`) measure **170/0 stmts, 22/0 branch = 100.00%** line+branch. Every key claim independently reproduced. | worktree `agent-ad672613998f05d84`, commit `94261f7f`, branch `claude/raw-data-integrity-jlzt1n`; ran via `/home/user/bruteX/.venv` |
| pull-health | **PARTIAL** | Cannot uphold "proven" — the verifier's "partial" verdict is **correct**. PROVEN: SEAM + WATCH + ALERT logic ships and works — `test_observability_pull_health.py` (**20 passed**) and the breach→alert integration test (`test_breach_routes_to_pull_degraded_alert` PASSED) wiring a REAL `SweepAlerter.pull_degraded` as `on_degraded` and firing a real breach. NOT proven: the monitor's **production binding at the live pull build site** — that is a flagged fast-follow (the seam + watch logic ship, the binding does not). | `tests/unit/test_observability_pull_health.py`; `brutex/observability/pull_health.py`; `sweep_alerts.py::pull_degraded` |
| lake-write | **PROVEN** | Could not refute. The binding at `parquet_writer.py:335` is real — `with _partition_lock(dest), partition_write_lock(self._lake_root, dest):` wraps the read-modify-write. All 6 named tests exist and **PASS on re-run** (not skipped, not env-gated — polars 1.42.0 installed, so `importorskip` guards mask nothing); each ran by node-id. | `brutex/data/parquet_writer.py:335`; the 6 named partition-lock / lake-write-halt tests |
| rate-controller | **PROVEN** | Could not refute. Constants Read-confirmed: `constants.py:177` `GROWW_RATE_LIMIT_RPS_CEILING = 8.0`, `:154` `GROWW_RATE_LIMIT_PER_MIN = 500`. `test_groww_constants.py:75-79` asserts both literally PLUS `8*60 == 480` and `480 <= 500` (the per-minute-cap-not-overshot invariant). Honestly disclosed: reproduced on worktree branch `claude/eap-reply-followup-jlzt1n` (HEAD `50aeb316`), **not `main`**. | `brutex/adapters/groww/constants.py:154,177`; `tests/.../test_groww_constants.py:75-79`; worktree branch `claude/eap-reply-followup-jlzt1n` |
| o1-hotpaths | **PROVEN** | Could not refute. `tests/bench/_o1_tiers.py` implements a real, honest two-tier ceiling — `ratio_ceiling()` returns strict 1.1× ONLY under `BRUTEX_M4PRO_STRICT=1`, else the caller's robust shared default (3–10×); `cache_tier_ceiling()` caps cross-cache-tier paths at 3.0× strict. All four Golden-Rule-#5 gates import and exercise REAL production objects (not stubs). | `tests/bench/_o1_tiers.py` (`ratio_ceiling`, `cache_tier_ceiling`); the four GR#5 O(1) bench gates |
| coverage | **PROVEN** | Could not refute. Reproduced the EXACT CI command (`.venv/bin/python -m pytest --cov=brutex --cov-branch --cov-fail-under=100 --ignore=tests/bench tests/unit tests/integration tests/property`) with the CI env vars (`PYTHONHASHSEED=0`, `BRUTEX_SAFETY_FACTOR=0.6`, `BRUTEX_OHLC_RATE_LIMIT_RPS=3`, `BRUTEX_AUDIT_CHAIN_ENABLED=1`, `BRUTEX_REPRODUCIBILITY_STRICT=1`). Result byte-identical to the verifier's — 100% line+branch, fail-under gate satisfied. | CI pytest `--cov-fail-under=100` invocation, reproduced verbatim |
| resilience-batch | **PROVEN** | Attempted to refute but evidence holds. Checked specifically for the warned gap classes — pragma-no-cover wiring, vacuous tests, env-skips — and found none masking the claim. CONFIRMED (#885): `atomic_io.py` + `errors.py` **100% line+branch**; both `test_merge_read_oom_halts` pass (real `MemoryError` → `LakeWriteHalt` `reason=out_of_memory`); `_GLOBAL_STOP_ERRORS` includes `LakeWriteHalt`. | `brutex/data/atomic_io.py`, `brutex/data/errors.py`; `test_merge_read_oom_halts`; `_GLOBAL_STOP_ERRORS` |
| dashboard-harness | **PARTIAL** | Cannot uphold full "proven". Env independently rebuilt (py3.12 venv, polars 1.42.0, duckdb 1.5.4, fno_math + trading_core wheels — confirmed). **Caching half PROVEN**: `test_universe_cache.py` + `test_dashboard_nav_latency_cache.py` → **15 passed in 5.10s**; `brutex/dashboard/universe_cache.py` = **65 stmts / 0 miss / 12 branch / 0 partial = 100%**. The verifier's broader "dashboard-harness" claim is only PARTIALLY upheld — the caching half reproduces exactly; the remainder of the broader harness claim was not fully reproduced. | `tests/unit/test_universe_cache.py`; `tests/integration/test_dashboard_nav_latency_cache.py`; `brutex/dashboard/universe_cache.py` |

---

## 2. Gaps & next

There are **0 GAP** subsystems and **2 PARTIAL** subsystems. Neither PARTIAL is
being represented as proven anywhere in this report.

### pull-health — PARTIAL (final downgraded from "proven")
- **Proven, safe to cite:** the SLA-breach detection seam, the watch rules, and
  the breach→Telegram-alert routing all ship and are test-backed
  (`test_observability_pull_health.py` 20 passed; `test_breach_routes_to_pull_degraded_alert`
  PASSED with a REAL `SweepAlerter.pull_degraded`).
- **NOT proven / not shippable as a claim:** the `PullHealthMonitor` is **not
  bound at the live pull build site**. As designed and documented, this binding
  is a flagged fast-follow — the seam + watch logic ship, the production binding
  does not. So today a real production pull does **not** yet emit these health
  gauges/alerts end-to-end.
- **To close:** wire the `monitor` param into the live `_observe_rate` seam at
  the production build site, then capture an end-to-end run showing a real
  breach producing a real alert from the production pull path (not just the
  integration test). Until that captured artifact exists, this stays PARTIAL.

### dashboard-harness — PARTIAL (final downgraded from "proven")
- **Proven, safe to cite:** the caching half — `universe_cache.py` at 100%
  line+branch (65/0 stmts, 12/0 branch) and the nav-latency + universe-cache
  test files (15 passed in 5.10s) reproduce exactly in a freshly rebuilt env.
- **NOT proven:** the broader "dashboard-harness" claim beyond the caching half
  was not fully reproduced by the independent verifier. Only the caching
  component cleared adversarial re-check.
- **To close:** identify and enumerate the non-caching components folded into the
  original "dashboard-harness" claim, then either reproduce each with a captured
  pytest + coverage result or scope the claim down to "dashboard caching layer"
  (which IS proven). Until the broader scope is reproduced, cite only the
  caching layer.

### Single most important thing NOT yet proven
**pull-health end-to-end in production:** the health monitor's logic is proven in
isolation, but it is **not bound at the live pull build site**, so a real
production pull does not yet self-instrument or alert. The detection + alerting
plumbing exists and is tested; the production wiring is the missing link, and no
captured end-to-end-from-production artifact exists to back it.

---

## 3. Honesty envelope (Golden Rule #10)

- "PROVEN" here means: an independent verifier attempted to REFUTE the claim and
  could not, AND reproduced the load-bearing result (test pass count and/or
  coverage numbers and/or the cited file:line binding) from a real checkout.
- Two rows (rate-controller, dashboard-harness, and the worktree for
  raw-integrity) were reproduced on **feature worktrees / branches, not `main`** —
  disclosed in-row. A claim is only as current as the branch it was proven on;
  re-verify against `main` before any release-grade external claim.
- The two PARTIAL rows are PARTIAL. Do not cite them as "fully proven",
  "guaranteed", or "100%". State what is green (the logic / the caching layer)
  and what is open (the production binding / the broader harness scope).

---

## 4. Re-verified on main (date 2026-06-26)

The two rows whose original proof was captured on **feature worktrees / branches
rather than `main`** (raw-integrity's worktree, rate-controller's branch) were
RE-VERIFIED on a clean checkout of `origin/main` (HEAD `0676100e`). Both hold —
no discrepancy. Run with the repo `/home/user/bruteX/.venv` (Python 3.12.3).

| Subsystem | Re-verified on main | Captured result |
|-----------|---------------------|-----------------|
| raw-integrity | **PROVEN-ON-MAIN** | `tests/unit/test_raw_integrity.py` + `tests/unit/test_raw_integrity_store.py` + `tests/integration/test_raw_integrity_wiring.py` → **49 passed**. Coverage of the 3 modules: `integrity_verify.py` 31/0 stmts · 4/0 branch · **100.00%**; `raw_integrity.py` 53/0 · 4/0 · **100.00%**; `raw_integrity_store.py` 86/0 · 14/0 · **100.00%** (TOTAL 170/0 stmts, 22/0 branch = **100.00%**, `--cov-fail-under` 100 reached). Live-pull binding confirmed present on main (see below). |
| rate-controller | **PROVEN-ON-MAIN** | `constants.py:177` `GROWW_RATE_LIMIT_RPS_CEILING: Final[float] = 8.0`; `constants.py:154` `GROWW_RATE_LIMIT_PER_MIN: Final[int] = 500`. `tests/unit/test_groww_constants.py` + `tests/unit/test_rate_common.py` → **73 passed**. |

### Live-pull capture-hook binding on main (cited file:line)
The `raw_integrity_store.make_capture_hook_factory` production binding exists at
all three live build sites on `origin/main`:

- `brutex/dashboard/pull_controller.py:624` (`from brutex.data.raw_integrity_store import make_capture_hook_factory`), wired at **`:644`** — `capture_hook_factory=make_capture_hook_factory(lake_root)`.
- `brutex/cli.py:1732` (import), wired at **`:1743`** — `capture_hook_factory=make_capture_hook_factory(lake_root)`.
- `brutex/adapters/groww/option_ingest.py:850` (`capture_hook_factory` param), consumed at **`:977-978`** — `if contract_hook is None and capture_hook_factory is not None: contract_hook = capture_hook_factory(...)`.

### Honesty note
Both re-verified rows match their original captured numbers exactly (raw-integrity
49 passed + 100% on 3 modules; rate-controller constants 8.0 / 500 + 73 passed),
so the in-row "reproduced on a feature branch, not `main`" caveat in §1/§3 is now
DISCHARGED for these two. The two PARTIAL rows (pull-health, dashboard-harness)
are UNCHANGED by this re-verification and remain PARTIAL — pull-health's logic is
proven + test-backed but its live binding landed separately (the flagged
fast-follow); dashboard-harness's caching half is proven (100%) while the broader
harness scope is only partially reproduced. Headline stays **6 PROVEN / 2 PARTIAL
/ 0 GAP**.
