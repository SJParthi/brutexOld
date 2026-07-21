# brutex-observability

The observability DECISION-law kernel (`DEC-RUST-OBSERVABILITY-001`) —
the four PURE decision laws of `brutex/observability/` re-hosted in Rust
behind the default-OFF `BRUTEX_OBSERVABILITY_RUST` facade
(`brutex/observability/observability_rust.py`), with Python retained as the
LIVE parity oracle:

* **Law A** — the loop-watchdog tiered OK→WARNED→CRITICAL→recovered stall
  state machine (`loop_watchdog.check_once`).
* **Law B** — the per-worker HEALTHY↔HUNG decision (`watchdog.Watchdog`).
* **Law C** — the SLA predicate + hysteresis edge (`sla.SLADetector`).
* **Laws D1/D2** — the stage-stamp strict-parse acceptance ORDER + the
  `validate_stage_stamp` verdict matrix (`stage_stamp`).

SCALARS ONLY cross the FFI (decide-in-Rust, materialize-in-Python — the
`DEC-FEED-RUST-CORE-001` shape). `json.loads` + `content_hash_of` stay
Python (the parse twin is the named follow-up
`DEC-RUST-OBSERVABILITY-STAMP-PARSE-003`; `run_tripwires` is
`DEC-RUST-OBSERVABILITY-TRIPWIRES-002`).

**Honest envelope (ADR-0020 banner, verbatim):** expected payoff ≈ 0 — the
conversion is for uniformity, not speed. Every routed law is
nanosecond-scale in Python; the FFI hop is a small SLOWDOWN, acceptable
because the seams run per monitor tick / per validate call and ship
DEFAULT-OFF.

## Build

```bash
# Pure-Rust battery (no libpython linked):
cargo test --release --locked

# The abi3-py312 wheel into the active venv:
maturin develop --release
```
