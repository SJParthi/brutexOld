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

Given a config-driven instrument universe and a Groww auth token,
BruteX:

1. **Ingests** historical OHLC bars for the 4 Track 1 indexes from the
   Groww Trade API (`/v2/charts/intraday` for 1m bars,
   `/v2/charts/historical` for daily). Pulls are **incremental** —
   subsequent runs only fetch new bars since the last successful
   timestamp per (instrument, timeframe).
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
| **Range** | Rolling `2021-01-01 → today` |
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
| L1 — Adapter | Groww HTTP client + envelope parser | `brutex/adapters/groww/` (Phase 2) |
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

**Clone and run the dashboard — one command:**

```bash
git clone https://github.com/SJParthi/bruteX.git
cd bruteX
make run            # turnkey: venv + libs + Rust kernel + launches the server
#   → open http://127.0.0.1:9080
```

`make run` is self-contained and idempotent (DEC-CLONE-RUN-002). From a
clean clone it: creates a `.venv/`, installs the `[data,ui]` runtime
extras, builds the `fno_math` Rust kernel if it is not importable (needs
`cargo`; the engine's `brutex.strategies.combo` imports it at module top),
then launches the dashboard. Re-running skips work already done. It is
roughly equivalent to:

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[data,ui]"
( cd rust/fno-math && maturin develop --release )   # build fno_math kernel
python -m brutex dashboard                           # alias: brutex run
```

For a full development environment (all extras + Rust toolchain + the
`brutex doctor` smoke), run `make setup` (wraps `scripts/setup/bootstrap.sh`).

Every run starts with a **preflight** (`scripts/run/preflight.sh`,
DEC-RUN-PREFLIGHT-001) that verifies the three host tools `make run` is built
on — `python3.12`, `make`, `cargo` — and, if one is missing, prints the exact
one-line install command for your OS instead of crashing. Everything the
project owns (venv, libraries, Rust kernel) is then installed automatically.
The only manual step that can ever exist is installing one of those three base
tools on a bare machine — a process cannot install the interpreter it runs on.

**In IntelliJ IDEA / PyCharm — one click, nothing to configure:** open the
cloned folder and pick **`brutex (one-click · make run)`** from the Run
dropdown (top-right ▶). It ships with the repo (`.idea/runConfigurations/`)
and runs `make run` in a terminal tab — venv, libs, Rust kernel, and the
dashboard are all built/launched for you. Prerequisites on the machine:
`python3.12`, `make`, and `cargo` (for the one-time kernel build). The older
`BruteX` Python config also exists, but it assumes the interpreter + deps are
already set up — use the one-click for a fresh clone.

> **What you'll see today:** the **Strategy Report** dashboard renders
> immediately, but its numbers are **placeholders**. Real values appear
> only after (a) data is fetched into the lake (needs Groww credentials,
> Phase 2) and (b) the brute-force engine runs. The page, the live
> `/api/v1/state` poller, `/metrics`, and the `/ws` socket are all wired
> and working; the data-browser routes (`/api/v1/instruments` etc.)
> return `503` until you start with `--lake-root <populated-lake>`.

**Optional — Groww credentials for live data pulls (Phase 2):**

```bash
export BRUTEX_GROWW_ACCESS_TOKEN="<your-token-from-groww.co>"
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

### Logs — the `/logs` web viewer + `LOG_DIR` knob

The dashboard ships a live log viewer at **`http://127.0.0.1:9080/logs`**
(also reachable from the nav bar on `/universe`, `/db`, `/credentials`). It
tails the same rotating JSON log file the runtime writes.

By default that file lives at `~/.brutex/logs/brutex.log`. To redirect it
**into the repo tree** so an IDE such as IntelliJ/IDEA can tail it, pass the
`LOG_DIR` knob — it exports `BRUTEX_LOG_DIR` for the run:

```bash
make run LOG_DIR=./.brutex-logs   # log → ./.brutex-logs/brutex.log
```

`./.brutex-logs/` is git-ignored. The knob is opt-in only — omitting it keeps
the `~/.brutex/logs` default unchanged. (`BRUTEX_LOG_FILE` overrides the full
path directly if you need a specific filename.)

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
- Optuna (CMA-ES) for Stage 3 Bayesian refinement
- Hypothesis, pytest-benchmark, mutmut, cargo-mutants, cargo-fuzz
- FastAPI + structlog + Prometheus
- Claude Code (Anthropic) as the build copilot, with a 4-agent
  hostile-review pattern at each phase end
