# BruteX — Claude Code Project Rules

> Persistent instructions for Claude Code working in this repository.
> Every Claude session in `bruteX/` must read and honor this file.

## 1. Project Identity

- **Name:** BruteX
- **Purpose:** Extreme drill-down brute-force backtesting engine for Indian F&O (NSE cash + FNO), globally extensible in later phases.
- **Data source (v1):** Groww API — **only** two surfaces:
  1. `https://groww.in/trade-api/docs/python-sdk/instruments` (instrument master CSV)
  2. `https://groww.in/trade-api/docs/python-sdk/backtesting` (historical candles)
- **Out of scope v1:** Live feed, WebSocket, orders, portfolio, funds, margins-live, commodity segment.
- **Primary deliverable phase (current):** `docs/REQUIREMENTS.md` — no code yet.

## 2. Golden Rules (never violate)

1. **No hallucination.** Every factual claim about Groww, NSE, SPAN, or any external system must be traceable to a verified source. If unsure, say "unverified" and stop.
2. **No premature code.** Do not write implementation code until `docs/REQUIREMENTS.md` is signed off by the user.
3. **Single source of truth.** All functional / non-functional requirements live under `docs/REQUIREMENTS.md` with IDs (`FR-###`, `NFR-###`, `MG-###`). Every later PR references an ID.
4. **Scope lock.** Groww surface is frozen to `instruments` + `backtesting`. Do not introduce live feeds, order placement, or WebSocket.
5. **Reproducibility first.** Every run is identified by `sha256(strategy ‖ params ‖ data_snapshot_hash ‖ code_sha)`. No computation without that hash recorded.
6. **O(1) discipline.** Bar lookup, combo dedup, and result cache must be O(1). Any regression fails CI.
7. **Rate-limit safety.** Groww OHLC bucket is ~25 req/s. BruteX adapter caps at **15 req/s effective** (0.6 safety factor). Never raise without explicit user approval.
8. **Idempotence.** Every ingest and every backtest combo must be rerun-safe. Same inputs → same outputs, bit-for-bit.
9. **Audit trail.** Every material action (ingest chunk, combo run, error, kill) appends one line to a hash-chained JSONL audit log. Breaking the chain fails CI.
10. **Honest limits.** If an SLA cannot be met under the current hardware envelope, SAY SO. Do not promise "millions in minutes" without naming the hardware and strategy envelope.

## 3. Branching and Commits

- **Active feature branch:** `claude/capture-system-requirements-F9IHZ`
- **Never push to `main`** without explicit user instruction.
- **Commit style:** Conventional Commits.
  - `docs(requirements): add FR-### data-lake section`
  - `feat(adapter): implement Groww instrument snapshot pull`
  - `fix(engine): O(1) regression in bar lookup`
  - `test(bench): add 1M combos SLA benchmark`
  - `chore(claude): update CLAUDE.md rules`
- **Never skip hooks** (`--no-verify` is forbidden unless the user explicitly requests).
- **Never force-push** shared branches.
- **Never amend** published commits; always create new commits.

## 4. Coding Standards (when code phase starts)

- **Language:** Python 3.12 primary; Rust (PyO3) for hot kernels; no other languages without ADR.
- **Formatters:** `ruff format` (not black); `ruff check --fix`.
- **Types:** `mypy --strict` and `pyright` both must pass.
- **Linters:** `ruff`, `pylint` (warnings-as-errors), `bandit` (security).
- **Tests:** `pytest`, `hypothesis` (property), `pytest-benchmark` (SLA), `mutmut` (mutation).
- **Coverage gates:** 100% line + 100% branch on `brutex/` package (test files excluded). Mutation score ≥ 85%.
- **Docstrings:** every public symbol. NumPy style.
- **No `print`** — always `structlog`.
- **No bare `except`** — specific exceptions only.
- **No global mutable state** — inject dependencies.
- **No `pandas` in hot paths** — use `polars` or `numpy` + `numba` or Rust.

## 5. Directory Conventions

```
bruteX/
├── CLAUDE.md                  ← this file
├── README.md
├── .claude/
│   ├── settings.json          ← hooks, permissions, env
│   └── agents/                ← project subagents (later)
├── docs/
│   ├── REQUIREMENTS.md        ← FROZEN source of truth
│   ├── adr/                   ← architecture decisions
│   └── runbooks/
├── brutex/                    ← Python package (P1+)
├── rust/                      ← Rust hot kernels (P3+)
├── tests/
│   ├── unit/
│   ├── property/
│   ├── golden/
│   ├── scenario/
│   └── bench/
├── ops/
│   ├── docker/
│   ├── k8s/
│   └── grafana/
└── .github/workflows/
```

## 6. Verified Groww Facts (cite these, don't re-derive)

| Fact | Value | Source |
|------|-------|--------|
| Instrument CSV URL | `https://growwapi-assets.groww.in/instruments/instrument.csv` | Groww docs |
| Instrument CSV columns | 21 (see REQUIREMENTS §Data Model) | Groww docs |
| Historical endpoint | `GET /v1/historical/candle/range` | Groww docs |
| SDK method (current) | `get_historical_candles(exchange, segment, groww_symbol, start_time, end_time, candle_interval)` | python-sdk/historical-data |
| SDK method (deprecated) | `get_historical_candle_data` — do NOT use | python-sdk/historical-data |
| Backtest segments | `CASH`, `FNO` only (no commodity) | Groww docs |
| FNO history depth | From 2020 onward | Groww docs |
| OHLC rate limit | ~25 req/s (shared bucket with quote) | marketcalls/stratzy |
| Time format | `YYYY-MM-DD HH:mm:ss` | Groww docs |
| PyPI package | `growwapi` (v1.5.0, Python 3.9–3.13 as of Dec 2025) | PyPI |

## 7. Hardware Envelope (for SLA claims)

**Reference runner:** 16 physical cores, 64 GB RAM, NVMe SSD, Linux x86_64.

All NFR throughput claims in `REQUIREMENTS.md` are valid **only** on this envelope with the specified strategy complexity bound. Larger machines scale linearly; smaller machines must document their own SLA profile.

## 8. When in Doubt

1. Re-read `docs/REQUIREMENTS.md`.
2. If answer not there → ask the user with `AskUserQuestion`.
3. Never guess on scope. Never expand scope silently.
4. Never propose "improvements" outside the requested task.

## 9. Session Etiquette

- Start of every session: read this file + the "Finalized Decisions" section of `REQUIREMENTS.md`.
- Use the `TodoWrite` tool for any multi-step task.
- Split long writes into multiple `Write` tool calls to avoid stream idle timeouts.
- Report honestly on failures; do not paper over errors.

---

_Last updated: 2026-04-14._
