# Synthetic adapter (`brutex/adapters/synthetic/`)

A **synthetic, deterministic, OFFLINE** market data source
(DEC-SYNTHETIC-ADAPTER-001). It satisfies the same `MarketAdapter` contract as
the live Groww adapter, but it **invents** realistic-looking candles on your own
machine — so the engine, ingest, and dashboard can run end-to-end with **no
credentials and no network**.

> ⚠️ **Honesty (CLAUDE.md Golden Rule #1/#10).** This data is **GENERATED for
> testing and demos**. Its `market_id` is `"synthetic"` and its `display_name`
> says `SYNTHETIC (offline, deterministic — NOT real market data)`. It must
> never be presented or stored as real market data.

## Why it exists

| You care about | 🟢 Synthetic (this package) | 🔵 Groww (live adapter) |
|---|---|---|
| Internet required | No | Yes |
| Login + phone OTP | No | Yes |
| API rate limits / cost | None | Yes |
| Same result every run | Yes — bit-for-bit identical | No (live data moves) |
| Real holidays/weekends | Yes (real NSE/BSE calendar) | Yes |
| Realistic price path | Yes (smooth value-noise + wicks) | Yes (it is real) |
| Real market data | **No — clearly labelled** | Yes |

It is also a richer superset of the older thin dry-run stub
(`ingest_common.synthetic_spot_bars`, a fixed 5-bar daily ramp under
`market_id="spot"`): this package is **seeded**, **multi-symbol**,
**multi-interval**, and covers **arbitrary date ranges**.

## What it generates

- **Universe:** `NSE-NIFTY`, `NSE-BANKNIFTY`, `BSE-SENSEX` (indices, zero
  volume) plus one clearly-labelled synthetic equity `NSE-SYNTH`
  (traded volume).
- **Intervals:** `1minute`, `5minute`, `15minute`, `1hour`, `1day`.
- **Sessions:** only inside real NSE/BSE trading sessions (regular / Muhurat /
  special-weekend), anchored at the 09:15 IST open and stored UTC.

## Determinism (Golden Rule #4 / #7)

Every bar is a stable `blake2b` digest of `(seed, symbol, minute-ordinal)` — no
global RNG, no wall-clock, no salted `hash()`. The same
`(seed, symbol, interval, start, end)` yields **bit-identical** bars across
processes and interpreter restarts. Generation is **O(1) per bar** (a bar
depends only on its own timestamp; no scan of earlier bars).

## Usage

```python
from datetime import date

from brutex.adapters.synthetic import SyntheticMarketAdapter
from brutex.models import Exchange

adapter = SyntheticMarketAdapter(seed=20210101)  # seed is explicit + reproducible

bars = adapter.fetch_ohlcv(
    symbol="NIFTY",
    exchange=Exchange.NSE,
    interval="1minute",
    start=date(2021, 1, 4),
    end=date(2021, 1, 5),
)
# -> 375 ascending, tz-aware-UTC, Decimal-priced CandleBar rows for one session.
```

Because it implements `MarketAdapter`, it is a drop-in for the vendor-neutral
ingest spine — see `tests/integration/test_synthetic_end_to_end.py`, which runs
the real spine fully offline.

## Tests

All 7 test types live under `tests/`: unit, property, golden, bench, stress,
integration, and mutation-by-construction (the golden digest pins exact output).
The package is held at 100% line + branch coverage.
