# BruteX

**Extreme drill-down brute-force backtesting engine** for Indian spot
indexes (NIFTY 50 / BANKNIFTY / SENSEX) + India VIX. Built in Python
3.12 with Rust (PyO3) hot kernels for shared mathematical cores.

Track 1 is **signal-only research** — it brute-forces ~2.9 million
strategy patterns (K=2..5 atomic conditions from a 52-condition
vocabulary) across 21 timeframes × 4 instruments × 2 exit modes, then
applies a 4-filter cascade (Deflated Sharpe → CPCV → Walk-Forward →
Lockbox) to return the top 3 production candidates per sweep.

---

## 🎯 What this is

Given a config-driven instrument universe and Groww TOTP credentials,
BruteX:

1. **Ingests** historical OHLC bars for the 4 Track 1 indexes from the
   Groww Trade API (1-minute candles + daily candles). Pulls are
   **incremental** — subsequent runs only fetch new bars since the last
   successful timestamp per (instrument, timeframe).
2. **Resamples** the 1-minute base into 19 derived intraday timeframes
   (2m, 3m, …, 15m, 30m, 60m, 120m, 180m, 240m), all anchored to 09:15
   IST, in a single deterministic pass.
3. **Persists** everything in a content-addressed Parquet + DuckDB +
   LMDB lake with hash-chained JSONL audit logs. Every API call, every
   state transition, every byte is reproducible and tamper-evident.
4. **Sweeps** ~33 billion (pattern × parameters) combinations via a
   5-stage pipeline (1A pattern discovery → 1B expansion → 2 full
   sweep → 3 Bayesian refine → 4 filter cascade), using Rust + Rayon +
   SIMD for the hot path.
5. **Reports** the surviving top 3 candidates with full statistical
   guards against overfitting (Deflated Sharpe with effective-N
   clustering, Combinatorial Purged Cross-Validation, Walk-Forward
   Optimization, and a held-out Lockbox window).

Track 1 produces **signal-only** output (`paper_pnl_paisa`). Execution
realism (brokerage, STT, slippage, IV/theta) re-enters in the
post-Track-1 **Options Track**, when Track 1 winners are translated
to ATM/ITM option fill simulations.

---

## 🔬 Track 1 scope (locked)

| Aspect | Value |
|---|---|
| **Instruments** | NIFTY 50, BANKNIFTY, SENSEX, India VIX |
| **Data source** | Groww Trade API |
| **Timeframes** | 21 (1m base + 19 derived intraday + 1d) |
| **Range** | Rolling `2020-01-01 → today` |
| **Hardware reference** | Apple M4 Pro (14-core, 10 P-cores allocated) |
| **Per-sweep SLA** | 2-4 days wall time on M4 Pro |
| **Verification** | 9-layer pyramid + 24 property invariants + 3 oracle tests |
| **Tradability** | None (signal-only) |
| **Brokerage / STT / GST / stamp** | NONE applied (indexes don't trade) |

Out of scope: futures, options, F&O contracts, live feed, WebSocket,
multi-tenant SaaS, commodity segment, BSE cash equity (other than the
SENSEX index value itself).

See `docs/TRACK1_BACKTEST_SPEC.md` for the master spec and
`docs/TRACK1_DATA_INGEST_DESIGN.md` for the data pipeline design.

---

## 🏗️ Architecture

```
   ┌─────────────────────────────────────────────────────────────┐
   │                                                             │
   │   Groww API  ─→  Adapter  ─→  Parser  ─→  Validate  ─→       │
   │                                                             │
   │   ─→ Parquet (atomic)  ─→  Watermark advance (DuckDB)       │
   │                                                             │
   │   ─→ Resample 1m → 19 intraday TFs  ─→  Audit verify        │
   │                                                             │
   │   ─→ Brute-force engine (Rust + Rayon + SIMD)               │
   │                                                             │
   │   ─→ Stage 1A/1B/2/3/4 sweep cascade                        │
   │                                                             │
   │   ─→ Top 3 production candidates + full audit trail         │
   │                                                             │
   └─────────────────────────────────────────────────────────────┘
```

Layers (broker-agnostic above L1):

| Layer | What | Code |
|---|---|---|
| L1 — Adapter | Groww SDK client + envelope parser | `brutex/adapters/groww/` |
| L2 — Data lake | Parquet + DuckDB + LMDB | `brutex/data/` |
| L3 — Feed | Replay + resampler | `brutex/feed/` |
| L4 — Execution | Signal-only simulator | `brutex/execution/` |
| L5 — Engine | Rust hot kernel | `rust/fno-math/` |
| L6 — Orchestrator | FSM + audit + rate limiter | `brutex/orchestrator/` |
| L7 — UI | FastAPI dashboard | `brutex/dashboard/` |

Everything above the adapter is **broker-agnostic**. To add a new
data source later, write a new adapter that implements the protocol
in `brutex/adapters/protocol.py`. Everything else works unchanged.

---

## ⚙️ Setup

**Clone → running dashboard — one command (DEC-ONE-COMMAND-UP-001):**

```bash
git clone https://github.com/SJParthi/bruteX.git
cd bruteX
./up.sh             # ANY state → running dashboard ready to pull, idempotently
#   → open http://127.0.0.1:9080
```

`./up.sh` is idempotent and re-runnable from ANY state — a fresh clone, a
tenth clone, a months-stale checkout, a half-broken venv. It: finds
`python3.12` (exact per-OS install hint if absent); creates or **self-heals**
`.venv/` (a stale/broken venv is rebuilt — user data under `~/.brutex` and the
lake are never touched); installs the runtime + pull extras
(`[dev,data,ui,groww,token-broker]`) and verifies the pull-critical deps
(`boto3` / `growwapi` / `pyotp`) one PASS/FAIL line each; builds the
`fno_math` + `trading_core` Rust kernels when missing (needs `cargo` once —
they are required, `brutex.indicators.*` import `trading_core` at module top);
decides the data-lake location with **zero input** (an existing lake is
auto-detected and recorded permanently, else the default `~/.brutex/lake` is
created — you never type a path) and documents the boot auto-resume opt-in
(**OFF by default** by operator directive — set `BRUTEX_AUTO_RESUME=1` in
`~/.brutex/.env` to enable crash-recovery auto-resume; the legacy sub-gate is
pre-armed so that one flip suffices), both recorded **once** in
`~/.brutex/.env` (loaded at every boot — no per-session exports ever again);
runs a self-check chain (deps → AWS identity → Groww daily token readable from
AWS SSM, strictly read-only — **it never mints a token** — → lake root
writable), each line PASS or the exact cause + fix; then boots the dashboard
via the existing `python -m brutex run` entry. Re-running while the dashboard
is up prints `already ready` and exits in about a second. Without AWS
credentials the checks degrade honestly: the dashboard still runs, a live pull
needs the fix each FAIL line names (`aws configure`; token minting lives in
`deploy/aws_token_minter/`).

**ONE entry point — never choose (DEC-ONE-ENTRY-UNIFY-001):** click ▶ Run
(**BruteX**) in IntelliJ IDEA / PyCharm, or run `make run`, or run `./up.sh` —
**all three are the same thing.** Each executes the ONE `./up.sh` path
(self-check → self-heal → start dashboard); when everything is already up it
fast-paths to `already ready` in about a second. The boot output prints the
dashboard URL (`http://127.0.0.1:9080`) and the rotating log-file location
(default `~/.brutex/logs/brutex.log`), one clear line each.

**In IntelliJ IDEA / PyCharm — one click, nothing to configure:** open the
cloned folder and pick **`BruteX`** from the Run dropdown (top-right ▶). It
ships with the repo (`.idea/runConfigurations/BruteX.xml`) and runs `./up.sh`
in a terminal tab — the same unified entry as `make run`. It is the ONLY
committed run configuration; if your local IDE still shows a stale
`brutex (one-click · make run)` entry from an earlier checkout, remove it via
Run → Edit Configurations (locally-created IDE state cannot be deleted by a
repo change).

### Appendix — manual setup (CI / debugging reference)

```bash
make run            # ONE entry: delegates to ./up.sh (identical to the IDE ▶ BruteX config)
make run-raw        # RAW direct entry: venv + libs + Rust kernels + launch, NO up.sh self-checks
#   → open http://127.0.0.1:9080
```

`make run-raw` preserves the pre-unification direct body — self-contained and
idempotent (DEC-CLONE-RUN-002) — for CI reproduction and debugging. From a
clean clone it: creates a `.venv/`, installs the `[data,ui,groww]` runtime
extras, builds the `fno_math` + `trading_core` Rust kernels if they are not
importable (needs `cargo`; `brutex.strategies.combo` and
`brutex.indicators.fib_levels` import them at module top),
then launches the dashboard. Re-running skips work already done. It is
roughly equivalent to:

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[data,ui,groww]"
( cd rust/fno-math && maturin develop --release )      # build fno_math kernel
( cd rust/trading-core && maturin develop --release )  # build trading_core kernel
python -m brutex dashboard                             # alias: brutex run
```

For a full development environment (all extras + Rust toolchain + the
`brutex doctor` smoke), run `make setup` (wraps `scripts/setup/bootstrap.sh`).

`make run-raw` starts with a **preflight** (`scripts/run/preflight.sh`,
DEC-RUN-PREFLIGHT-001) that verifies the three host tools it is built
on — `python3.12`, `make`, `cargo` — and, if one is missing, prints the exact
one-line install command for your OS instead of crashing (`./up.sh` performs
the same probes itself, with the same per-OS fix hints). Everything the
project owns (venv, libraries, Rust kernel) is then installed automatically.
The only manual step that can ever exist is installing one of those three base
tools on a bare machine — a process cannot install the interpreter it runs on.

> **What you'll see today:** the **Strategy Report** dashboard renders
> immediately, but its numbers are **placeholders**. Real values appear
> only after (a) data is fetched into the lake (needs Groww
> credentials) and (b) the brute-force engine runs. The page, the live
> `/api/v1/state` poller, `/metrics`, and the `/ws` socket are all wired
> and working; the data-browser routes (`/api/v1/instruments` etc.)
> return `503` until you start with `--lake-root <populated-lake>`.

**Optional — Groww credentials for live data pulls (AWS-SSM-only,
DEC-AWS-ONLY-CREDS-001):** the Groww api-key, TOTP secret AND the daily
access token live ONLY in AWS Systems Manager Parameter Store — SecureStrings
`/tickvault/prod/groww/api-key`, `/tickvault/prod/groww/totp-secret`,
`/tickvault/prod/groww/access-token` in region `ap-south-1`. There is NO
secret env var, NO credentials file, and NO in-app credentials page — the
read is fail-fast. A live pull therefore needs usable AWS credentials
(`aws configure` / `AWS_PROFILE` / an instance role); the SSM paths + region
are overridable via `BRUTEX_TOKEN_PARAM_API_KEY` /
`BRUTEX_TOKEN_PARAM_TOTP_SECRET` / `BRUTEX_TOKEN_PARAM_ACCESS_TOKEN` /
`BRUTEX_TOKEN_AWS_REGION` (defaults above). Full setup:
`docs/runbooks/aws-token-broker.md`; var reference:
`docs/runbooks/ENV_VARS.md` §5.

```bash
aws configure       # or export AWS_PROFILE=<profile-with-ssm-read-access>
export BRUTEX_LAKE_ROOT=/path/to/large/ssd/brutex-lake
brutex version      # verify install
```

---

## 🚀 Usage (Phase 2 onward)

The CLI surface lands in Phase 2 with the Groww adapter. Track 1 plan:

```bash
# Daily incremental pull (only fetches new bars since last run)
brutex pull

# View pipeline status
brutex status

# Verify audit chain integrity
brutex audit verify audit/audit.jsonl

# Dashboard (FastAPI; needs the [ui] extra — `make run` installs it)
brutex dashboard --lake-root $BRUTEX_LAKE_ROOT      # alias: brutex run
```

Current Phase 0 CLI exposes only:

```bash
brutex version
brutex status
brutex audit verify <file>
brutex dashboard          # alias: brutex run
```

### Logs — the rotating log file + `LOG_DIR` knob

The runtime writes a rotating JSON log file. By default it lives at
`~/.brutex/logs/brutex.log`, and every boot prints its location as one clear
`logs → …` line (the unified `make run` / `./up.sh` / IDE ▶ BruteX entry all
use this default — DEC-ONE-ENTRY-UNIFY-001). The unified entry ALSO maintains
an idempotent **`<repo>/logs` symlink** to that log directory
(DEC-LOGS-IN-PROJECT-001), so the live log folder + file are visible INSIDE
the IntelliJ/PyCharm project tree — click ▶ BruteX and watch `logs/brutex.log`
without leaving the IDE. The symlink is git-ignored, repointed if the log dir
moves, and a REAL directory named `logs` is never clobbered (a warning is
printed instead). To redirect it
**into the repo tree** so an IDE such as IntelliJ/IDEA can tail it, use the
RAW direct entry's `LOG_DIR` knob — it exports `BRUTEX_LOG_DIR` for the run
(the knob is scoped to `run-raw` + the `wipe` targets; the unified `run`
ignores it so all three entries stay identical):

```bash
make run-raw LOG_DIR=./.brutex-logs   # log → ./.brutex-logs/brutex.log
```

`./.brutex-logs/` is git-ignored. The knob is opt-in only — omitting it keeps
`run-raw`'s in-repo `./.brutex-logs` default. (`BRUTEX_LOG_FILE` overrides the
full path directly if you need a specific filename.)

---

## 📐 Guarantees

| Guarantee | Mechanism | Verified by |
|---|---|---|
| **O(1) latency** | LMDB + Bloom + DuckDB PK + mmap'd Parquet | Bench tests `p99(N=1M) / p99(N=1K) ≤ 1.1` |
| **Bit-for-bit reproducibility** | Atomic tempfile+rename writes, deterministic Parquet bytes | Audit-log replay oracle test |
| **No look-ahead bias** | Instrumented bar accessor | Property P-06 |
| **Uniqueness + dedup** | 4-layer defense: Watermark → Bloom → LMDB → bar_hash | Property tests + golden corpora |
| **Audit chain integrity** | Hash-chained JSONL with `verify_chain()` | Property P-16, P-17, P-20 |
| **Honest SLA** | Memory-bandwidth-bounded throughput math | `docs/TRACK1_PERFORMANCE_BUDGET.md` |
| **Statistical rigor** | DSR (Bailey-LdP effective-N) + CPCV + WFO + Lockbox | 4-filter cascade in Stage 4 |

See `docs/TRACK1_VERIFICATION_PYRAMID.md` for the full 9-layer test
pyramid + 24 property invariants.

---

## 🧪 Development

```bash
# Lint + type check
ruff check brutex tests
ruff format --check brutex tests
mypy --strict brutex/

# Run tests
pytest tests/unit              # ~530 tests
pytest tests/property          # 24 hypothesis invariants
pytest tests/bench             # O(1) ratio gates
pytest tests/golden            # frozen SHA256 fixtures

# Coverage
coverage run -m pytest && coverage report
```

All commits must follow Conventional Commits. See
`.github/workflows/ci.yml` for the full quality gate pipeline (~22
gates: lint, type, test, property, bench, golden, security, docs,
licenses, Rust, banned patterns, secrets, CodeQL, Semgrep, Trivy,
SBOM, mutation, etc.).

---

## 📚 Documentation

| File | Purpose |
|---|---|
| `CLAUDE.md` | Project rules + verified facts table |
| `docs/TRACK1_BACKTEST_SPEC.md` | Master spec (signal-only research, 21 TFs, 4 inst) |
| `docs/TRACK1_DATA_INGEST_DESIGN.md` | End-to-end ingest pipeline design |
| `docs/TRACK1_VERIFICATION_PYRAMID.md` | 9 layers + 24 invariants + 3 oracles |
| `docs/TRACK1_VOCABULARY.md` | 52 atomic conditions, K=2..5 ANDed |
| `docs/TRACK1_PHASE_PLAN.md` | 8-12 week build plan, Phase 0-4 |
| `docs/TRACK1_PERFORMANCE_BUDGET.md` | Honest 2-4 day SLA, memory BW math |
| `docs/TRACK1_MICROSTRUCTURE.md` | Pre-open, Muhurat, halts, rebalance |
| `docs/MIGRATION.md` | Decision ledger (#1-#102+) |
| `docs/REQUIREMENTS.md` | RETIRED (stub) — see Track 1 docs |
| `docs/ROADMAP.md` | RETIRED (stub) — see `docs/phases/00_MASTER_CHECKLIST.md` |

---

## 📜 License

See `LICENSE`.

---

## 🤖 Built with

- Python 3.12, Rust + PyO3, Arrow zero-copy
- Polars, DuckDB, LMDB, Parquet (Zstd-9)
- brutex-refine (in-repo Rust BIPOP-style CMA-ES) for Stage 3 refinement
- Hypothesis, pytest-benchmark, mutmut, cargo-mutants, cargo-fuzz
- FastAPI + structlog + Prometheus
- Claude Code (Anthropic) as the build copilot, with a 4-agent
  hostile-review pattern at each phase end
