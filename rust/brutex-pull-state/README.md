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
- **The ONLY decision path (flip phase, fail-closed)**: consumed via the
  facade leaf `brutex/pull_state_rust.py` — the pre-flip Python oracles are
  DELETED and the `BRUTEX_PULL_STATE_RUST` opt-in knob is REMOVED (a SET
  value raises `PullStateFlagRemovedError`); a missing/stale wheel, a
  kernel raise, or a refused/poisoned result raises the typed
  `PullStateKernelError` — a pull decision STOPS, never a silent fallback.
- **Frozen contract**: the guard verdicts over the FULL covered NSE calendar
  are pinned by `tests/golden/test_market_hours_guard_golden.py` +
  `tests/golden/fixtures/market_hours_guard_verdicts.json`, and the Rust arm
  is differential-tested against in-test INLINE laws
  (`tests/property/test_pull_state_rust_parity.py` — the pre-flip Python
  oracle is deleted, so the differential partner is the inline law).
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
posture). The flip has SHIPPED: the Python oracles are deleted and these
kernels are the only decision path (fail-closed at the facade).
