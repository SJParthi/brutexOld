# fno-math

> **Single source of truth** for Greeks, indicators, and strategies shared
> between [tickvault](https://github.com/SJParthi/tickvault) (live F&O
> trading in Rust) and [brutex](https://github.com/SJParthi/bruteX)
> (brute-force backtesting in Python).

## Purpose

This crate exists to eliminate live-vs-backtest drift as a failure mode.
**Every mathematical formula, every indicator, every strategy in both
products comes from this one crate.** A change here automatically applies
to both products on the next version bump — there is no parallel Python
implementation to get out of sync.

## Scope

- **Greeks:** Black-Scholes, implied volatility calibration, put-call
  ratio, portfolio aggregation
- **Indicators:** SMA / EMA / RSI / MACD / BB / ATR / Supertrend / VWAP /
  OBI / and every future indicator
- **Strategies:** base `Strategy` trait + concrete momentum / mean-reversion /
  options-structure implementations
- **Math:** rolling stats, distributions, solvers

## Status

**Scaffold only.** The actual implementations live in tickvault's
`crates/trading/src/greeks/` and are extracted into this crate by
`../scripts/extract-fno-math.sh`. See
[`docs/runbooks/fno-math-extraction.md`](../../docs/runbooks/fno-math-extraction.md)
for the step-by-step extraction procedure.

## Consumers

### Rust (tickvault)

```toml
# tickvault/Cargo.toml
[dependencies]
fno-math = { path = "../brutex/rust/fno-math" }  # or a published version
```

```rust
use fno_math::greeks::black_scholes::delta;
use fno_math::indicators::rsi::Rsi;
use fno_math::strategies::Strategy;
```

### Python (brutex) via `fno-math-py` PyO3 wrapper

```python
from fno_math_py.greeks import delta, gamma, theta, vega, rho
from fno_math_py.indicators import sma, ema, rsi, macd
from fno_math_py.strategies import IronCondor, MomentumCrossover
```

## Build

```bash
# Rust side
cargo build --release

# Python wheel (from rust/fno-math-py/)
maturin develop   # local dev
maturin build --release   # production wheel
```

## Drift check

The `scripts/check-fno-math-drift.sh` script runs in BruteX CI and
fails if tickvault's `crates/trading/src/greeks/` has changed since
the last extraction. The operator resolves drift by re-running the
extraction script.

## License

Proprietary — owned by the operator. Both tickvault and BruteX inherit
this license.
