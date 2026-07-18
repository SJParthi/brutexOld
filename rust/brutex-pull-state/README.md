# brutex-pull-state

The dashboard pull-state DECISION kernel (DEC-RUST-PULL-STATE-001, lane
Phase 1) — a bit-identity-first Rust re-hosting of the pull-state /
rate-view decision cores, starting with the vendor-account SAFETY guard
`brutex/calendars/market_hours.py::pull_allowed_now`
(DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692).

- **Integers-only FFI** (the DEC-FEED-RUST-CORE-001 discipline):
  `pull_allowed_us(us_of_day: int, is_trading_day: bool) -> bool` —
  microsecond-of-day is order-isomorphic to Python `datetime.time`
  comparison, so the `[08:45, 15:35)` boundary law is exact. The NSE
  calendar (`brutex/calendars/nse.py`) stays Python — never re-minted here.
- **Default-OFF**: consulted only behind `BRUTEX_PULL_STATE_RUST` via the
  facade leaf `brutex/pull_state_rust.py`; the retained Python predicate is
  the live parity oracle. Flag OFF is byte-identical (spy-pinned).
- **Frozen contract**: the guard verdicts over the FULL covered NSE calendar
  are pinned by `tests/golden/test_market_hours_guard_golden.py` +
  `tests/golden/fixtures/market_hours_guard_verdicts.json`, and the Rust arm
  is differential-tested against the oracle
  (`tests/property/test_pull_state_rust_parity.py`).
- **Standalone crate** (no workspace — the sibling convention); pure-Rust
  core with the PyO3 surface behind the optional `python` feature
  (pyo3 0.29, abi3-py312, maturin mixed layout).

Build the wheel into the project venv:

```bash
cd rust/brutex-pull-state
maturin develop --release
```

Honest envelope (Golden Rule #10): the predicate is O(1) and already
nanosecond-scale in Python — this port serves the operator-ordered Rust
uniformity program, NOT speed (expected payoff ≈ 0; the ADR-0020 banner
posture). The default flip + Python-oracle deletion are LATER lane phases,
never this one.
