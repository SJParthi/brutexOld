# BruteX

**Extreme drill-down brute-force backtesting engine** for Indian F&O markets,
globally extensible. Built in Python 3.12 with Rust (PyO3) hot kernels for
mathematical cores shared with the [tickvault](https://github.com/SJParthi/tickvault)
live trading system — **one source of truth** for Greeks, indicators, and
strategies across live and backtest.

---

## 🎯 What this is

Given a trading strategy (Python class that exposes `on_bar` and returns
signals), BruteX:

1. **Ingests** historical data for every F&O-eligible Indian instrument from
   Groww via four strict sequential phases: instruments → expiries →
   contracts → candles.
2. **Persists** everything in a content-addressed LMDB store with hash-chained
   JSONL audit logs — every API call, every state transition, every byte is
   reproducible and tamper-evident.
3. **Sweeps** hundreds of thousands of parameter combinations using Ray
   actor pools + Rust hot kernels + bitmap prefilters + LMDB result caches.
4. **Reports** results with CPCV / walk-forward / PBO / Deflated Sharpe
   corrections so you never get fooled by overfitting.

The ingest + orchestration layer is what P1a Day 2 delivers. Strategy
engine, brute-force orchestrator, and dashboard refinement follow in
P2–P5.

---

## 📐 Architecture in one diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        brutex CLI                               │
└─────────────────────────────────────────────────────────────────┘
                               │
┌─────────────────────────────────────────────────────────────────┐
│   PhaseADriver → PhaseBDriver → PhaseCDriver → PhaseDDriver     │
│                (sequential gating per §8.5)                      │
└─────────────────────────────────────────────────────────────────┘
                               │
┌─────────────────────────────────────────────────────────────────┐
│  PhaseRunner ─ drive_item_to_terminal ─ retry backoff loop      │
│  PipelineEvent fanout → Telegram + Dashboard subscribers         │
└─────────────────────────────────────────────────────────────────┘
                               │
┌─────────────────────────────────────────────────────────────────┐
│                      GrowwAdapter                                │
│  AdaptiveTokenBucket + WorkItemStore + AuditWriter +             │
│  retry classifier + FSM transitions (§FR-ING-008/014)            │
└─────────────────────────────────────────────────────────────────┘
                               │
┌─────────────────────────────────────────────────────────────────┐
│                  growwapi.GrowwAPI (lazy import)                 │
└─────────────────────────────────────────────────────────────────┘
```

Everything above `GrowwAdapter` is **broker-agnostic**. To add a new
data source (Zerodha, Upstox, IBKR, Binance, Polygon), implement a
new adapter that satisfies `GrowwClientProtocol` + the `pull_*`
methods — nothing else changes.

---

## 🔒 Guarantees (mechanically enforced, not vibes)

Every guarantee below has a file + test that fails the build on regression.
See `docs/REQUIREMENTS.md §17` for the full list of 20 coverage gates
(CG-01 .. CG-20).

| Guarantee | Enforcement |
|---|---|
| **O(1) latency** | LMDB B+tree lookups via `brutex.orchestrator.store.WorkItemStore`. Microbenchmark in `tests/bench/` asserts median ≤ 500 ns. |
| **Uniqueness** | `content_hash = sha256(...)`. Collision probability 2⁻²⁵⁶. `tests/property/test_hash_no_collision.py` runs 10⁷ random params with zero collisions. |
| **Deduplication** | `WorkItemStore.exists_terminal` probed before every SDK call. Rerun-safe per `NFR-REL-001`. |
| **Never-skip** | Work-item FSM with 6 states (`PENDING` / `IN_FLIGHT` / `RETRYING` / `SUCCESS` / `NO_DATA` / `QUARANTINED`). Every exception maps to a terminal or retrying state. `tests/unit/test_orchestrator_state.py::TestNeverSkipInvariant` parametrized over every `Outcome` value. |
| **Reproducibility** | `PYTHONHASHSEED=0`, seeded RNGs, content-addressed hashing. Same inputs → byte-identical outputs. `NFR-DET-002`. |
| **Audit trail** | Hash-chained JSONL via `brutex.orchestrator.audit.AuditWriter`. `verify_chain` catches 7 tamper modes. Runs on every push via CI. |
| **Rate-limit safety** | `AdaptiveTokenBucket` at 15 req/s (0.6 × Groww's 25 req/s bucket). Auto-downshifts on 429 bursts per `FR-ING-017`. Never drops a request silently. |
| **Phase completeness** | `phase_complete()` invariant: every item in a terminal state before the next phase starts. `PhaseHaltError` raised on unresolved quarantine. |

---

## 🚀 Quick start

```bash
# Clone
git clone https://github.com/SJParthi/bruteX.git
cd bruteX

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Install pre-commit hooks (runs ruff + mypy + banned patterns + secrets)
pip install pre-commit && pre-commit install

# Verify the install
brutex version
```

Set your Groww API token (for real ingest):

```bash
export GROWW_API_AUTH_TOKEN="<your token from groww.in>"
```

Optional — enable Telegram alerts:

```bash
# Follow docs/runbooks/telegram-setup.md first to create the bot
export BRUTEX_TELEGRAM_BOT_TOKEN="123456789:ABC..."
export BRUTEX_TELEGRAM_CHAT_ID="12345"
export BRUTEX_TELEGRAM_ENABLED=true
```

---

## 💻 CLI commands

```bash
# Version + commit SHA
brutex version

# Status of the currently running ingest (reads ~/.brutex/runtime.json)
brutex status

# Run Phase A only — fetch + normalise + filter the Groww instrument CSV
brutex ingest instruments [--snapshot-date YYYY-MM-DD] [--dry-run]

# Verify a hash-chained audit log
brutex audit verify <path/to/audit.jsonl>

# Future phases (NOT YET WIRED via CLI)
brutex ingest expiries           # Phase B
brutex ingest contracts          # Phase C
brutex ingest candles            # Phase D
brutex ingest all                # sequential A → B → C → D
```

Dry-run mode uses a FakeGrowwClient — exercises the wiring without
any network calls. Perfect for CI smoke tests.

---

## 🗂️ Repository layout

```
bruteX/
├── brutex/                          # Python package (P1+)
│   ├── adapters/groww/              # L1 broker adapter
│   │   ├── client.py                # GrowwAdapter facade
│   │   ├── instruments.py           # PhaseADriver
│   │   ├── expiries.py              # PhaseBDriver
│   │   ├── contracts.py             # PhaseCDriver
│   │   ├── candles.py               # PhaseDDriver + chunker
│   │   ├── normaliser.py            # raw dict → Instrument
│   │   └── constants.py             # SDK mapping tables
│   ├── orchestrator/                # L6 orchestration
│   │   ├── state.py                 # Work-item FSM
│   │   ├── retry.py                 # Exception classifier + backoff
│   │   ├── audit.py                 # Hash-chained audit log
│   │   ├── store.py                 # LMDB WorkItem persistence
│   │   ├── rate_limiter.py          # Adaptive token bucket
│   │   └── pipeline.py              # Phase runner + gating
│   ├── notifications/telegram.py    # Async Telegram notifier
│   ├── dashboard/app.py             # FastAPI dashboard state + HTML
│   ├── observability/metrics.py     # Prometheus exporter
│   ├── config/ports.py, runtime.py  # Port management + discovery
│   ├── models.py                    # Exchange, Segment, Instrument, ...
│   ├── universe.py                  # V1_INDICES + is_in_scope filter
│   ├── math/                        # fno-math PyO3 re-exports (placeholder)
│   └── cli.py                       # brutex entry point
│
├── rust/                            # Shared Rust workspace
│   ├── fno-math/                    # The single source of truth
│   │   ├── src/greeks/              # Extracted from tickvault
│   │   ├── src/indicators/
│   │   ├── src/strategies/
│   │   └── src/compat/              # Vendored tickvault-common
│   └── fno-math-py/                 # PyO3 wrapper (maturin)
│
├── tests/
│   ├── unit/                        # ~3,400 lines, 250+ tests
│   ├── property/                    # Hypothesis (scaffold)
│   ├── golden/                      # Fixture-pinned (scaffold)
│   ├── scenario/                    # 25-row gauntlet (phase-by-phase)
│   └── bench/                       # O(1) proofs + SLA (P4)
│
├── docs/
│   ├── REQUIREMENTS.md              # v1.0 signed off, 1700+ lines
│   ├── REQUIREMENTS_SIMPLE.md       # Plain-English companion
│   ├── groww-sdk/                   # Verified Groww SDK reference
│   │   ├── instruments.md
│   │   ├── annexures.md
│   │   ├── backtesting.md
│   │   └── exceptions.md
│   └── runbooks/
│       ├── telegram-setup.md
│       └── fno-math-extraction.md
│
├── scripts/
│   ├── extract-fno-math.sh          # One-shot extractor
│   ├── check-fno-math-drift.sh      # Scheduled drift detector
│   └── ci/
│       ├── scan-banned-patterns.sh
│       └── check-conventional-commits.sh
│
├── .github/workflows/
│   ├── ci.yml                       # 13-gate aggregate pipeline
│   └── drift-check.yml              # Every 6h tickvault watcher
│
├── .pre-commit-config.yaml          # Local fast gates
├── CLAUDE.md                        # Project rules for Claude Code
└── pyproject.toml                   # Python tooling + deps
```

---

## 🧪 Testing

```bash
# Full unit test suite with 100% coverage gate (CG-01)
make test-cov

# Just unit tests, fast feedback
make test-unit

# Lint + type + security + all CG gates
make all
```

Every CI push runs 13 parallel gates via `.github/workflows/ci.yml`.
See the `ci-ok` aggregate check for the merge gate.

---

## 🔗 Cross-product — how BruteX and tickvault share code

BruteX and [tickvault](https://github.com/SJParthi/tickvault) are two
products, one shared source of mathematical truth. The `rust/fno-math/`
crate lives inside the BruteX repo and is consumed by:

- **Tickvault** (live trading, Rust) via a normal Cargo path dep or
  crates.io version.
- **BruteX** (backtesting, Python) via the `fno-math-py` PyO3 wrapper
  which builds a Python wheel containing the compiled Rust code.

**Any change to a Greek, indicator, or strategy lives in exactly one
place** — `rust/fno-math/src/` inside this repo — and both products
pick up the change on the next version bump.

Full details: `docs/REQUIREMENTS.md §7A Shared Core Library Contract`
and `docs/runbooks/fno-math-extraction.md`.

---

## 📖 Documentation

| Document | Purpose |
|---|---|
| [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) | Signed-off v1.0 spec — every FR / NFR / CG / ADR / MG |
| [`docs/REQUIREMENTS_SIMPLE.md`](docs/REQUIREMENTS_SIMPLE.md) | Plain-English walkthrough |
| [`docs/groww-sdk/`](docs/groww-sdk/) | Verified Groww SDK reference (4 files) |
| [`docs/runbooks/`](docs/runbooks/) | Operator procedures (Telegram, fno-math extraction) |
| [`CLAUDE.md`](CLAUDE.md) | Project rules read by every Claude Code session |

---

## 📜 License

Proprietary. See [`SECURITY.md`](SECURITY.md) and the license field
in `pyproject.toml`.

---

_Last updated: 2026-04-14. BruteX v0.1.0a0 — P1a Day 2 complete._
