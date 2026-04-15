# BruteX Adapters — the multi-market contract

> **Status:** Architectural runbook. Applies from PR #2 onward.
>
> **Audience:** Anyone adding a new market to BruteX — Polygon,
> Binance, Dhan, Zerodha, IBKR, Coinbase, or anything else.

## TL;DR

Adding a new market is a **one-file job**. You write a class that
satisfies :class:`brutex.adapters.protocol.MarketAdapter`, declare its
:class:`MarketCapabilities`, and the rest of BruteX — orchestrator,
FSM, audit log, rate limiter, LMDB store, dashboard, Telegram, data
lake, Rust math — **works unchanged**.

## The three questions every market must answer

1. **What instruments are tradable?** — `list_instruments(cursor)`
2. **What derivative contracts exist (if any)?** — `list_derivatives(underlying, exchange)`
3. **Give me OHLCV bars.** — `fetch_ohlcv(symbol, exchange, interval, start, end)`

That's the whole protocol. Everything else is the orchestrator's job.

## Why it's market-neutral

In PR #1 BruteX had a Groww-shaped protocol with method names
`get_expiries` / `get_contracts` / `get_historical_candles`. That was
fine when Groww was the only market, but it would force a crypto-spot
adapter to fake "expiries" it doesn't have, and a US-equities adapter
to fake "FNO contracts" it doesn't need.

PR #2 introduces `brutex/adapters/protocol.py` with a **semantic**
contract — three verbs that every market answers, plus a
`MarketCapabilities` descriptor that tells the orchestrator which
phases to run.

## Step-by-step: adding a new market

Say you want to add Polygon for US equities + options.

### Step 1 — create the package

```
brutex/
  adapters/
    polygon/
      __init__.py
      client.py         # Wraps polygon-api-client SDK
      market.py         # Implements MarketAdapter
      instruments.py    # Normaliser: Polygon row → Instrument
      contracts.py      # Normaliser: Polygon option contract → DerivativeContract
```

### Step 2 — declare capabilities

In `brutex/adapters/polygon/market.py`:

```python
from brutex.adapters.protocol import MarketCapabilities
from brutex.models import Exchange

POLYGON_CAPS = MarketCapabilities(
    market_id="polygon-us",
    display_name="Polygon.io US Equities + Options",
    exchanges=frozenset({Exchange.NSE}),  # or a US exchange enum
    has_derivatives=True,         # options chain
    has_expiries=True,            # weekly/monthly
    has_option_chains=True,       # CE/PE per strike
    supports_intraday=True,       # 1-minute bars
    earliest_history=date(2003, 1, 1),   # Polygon v2 aggs go back that far
    native_timezone="America/New_York",  # NYSE session
    rate_limit_rps=5.0,           # free tier; paid is higher
)
```

### Step 3 — implement the three methods

```python
class PolygonMarketAdapter:
    def __init__(self, client: PolygonClient, ...): ...

    @property
    def capabilities(self) -> MarketCapabilities:
        return POLYGON_CAPS

    def list_instruments(self, *, cursor=None) -> InstrumentPage:
        # Polygon's /v3/reference/tickers returns pagination via next_url
        response = self._client.list_tickers(market="stocks", cursor=cursor)
        instruments = tuple(
            normalise_polygon_ticker(row) for row in response.results
        )
        return InstrumentPage(
            instruments=instruments,
            next_cursor=response.next_cursor,
        )

    def list_derivatives(self, *, underlying_symbol, exchange):
        # /v3/reference/options/contracts filtered by underlying
        for contract in self._client.list_options_contracts(
            underlying_ticker=underlying_symbol
        ):
            yield DerivativeContract(
                underlying_symbol=underlying_symbol,
                expiry=contract.expiration_date,
                symbol=contract.ticker,
                contract_type="CE" if contract.contract_type == "call" else "PE",
                strike=contract.strike_price,
            )

    def fetch_ohlcv(self, *, symbol, exchange, interval, start, end):
        # /v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from}/{to}
        pl_interval, pl_multiplier = translate_interval(interval)
        response = self._client.get_aggs(
            ticker=symbol,
            multiplier=pl_multiplier,
            timespan=pl_interval,
            from_=start,
            to=end,
        )
        # Convert Polygon's ET bars to UTC via _NY_TZ → UTC
        return [
            CandleBar(
                timestamp=bar.timestamp.astimezone(UTC),
                open=Decimal(str(bar.open)),
                high=Decimal(str(bar.high)),
                low=Decimal(str(bar.low)),
                close=Decimal(str(bar.close)),
                volume=bar.volume,
                open_interest=None,  # Polygon equities have no OI
            )
            for bar in response.results
        ]
```

### Step 4 — register the adapter

Adapters don't need a registry — they're just classes. The CLI
accepts a `--adapter` flag:

```
brutex ingest candles --adapter polygon --symbol AAPL --interval 1minute \
    --start 2024-01-01 --end 2024-12-31
```

The CLI imports `brutex.adapters.polygon.market.PolygonMarketAdapter`,
instantiates it, and runs the orchestrator against it. Done.

### Step 5 — orchestrator skips inapplicable phases automatically

If your adapter has `has_derivatives=False` (e.g. spot crypto), the
orchestrator **automatically** skips Phase B (expiries) and Phase C
(contracts) and goes straight from Phase A (instruments) to Phase D
(bars). No code change needed in the orchestrator — it just reads
`adapter.capabilities.has_derivatives` at startup.

## What's common vs market-specific

| Concern | Location | Market-specific? |
|---|---|---|
| **Math / Greeks / indicators / strategies** | `rust/fno-math/` (Rust) | ❌ No — single Rust crate shared with `tickvault` |
| **Work-item state machine** | `brutex/orchestrator/state.py` | ❌ No — generic FSM |
| **Rate limiter** | `brutex/orchestrator/rate_limiter.py` | ❌ No — adapter passes its own RPS number |
| **Audit chain** | `brutex/orchestrator/audit.py` | ❌ No — generic hash-chained log |
| **LMDB dedup store** | `brutex/orchestrator/store.py` | ❌ No — content-addressed |
| **Data lake** | `brutex/data/` | ❌ No — Parquet + DuckDB, partition path is market-neutral |
| **Dashboard** | `brutex/dashboard/` | ❌ No — shows phases/work items by market |
| **Telegram** | `brutex/notifications/` | ❌ No — generic event bus |
| **Brute-force backtest engine (P3+)** | `rust/fno-math/` + `brutex/engine/` | ❌ No — takes any OHLCV stream |
| **SDK wrapper (groww/polygon/binance/…)** | `brutex/adapters/<market>/` | ✅ Yes — one file per market |
| **Instrument normaliser** | `brutex/adapters/<market>/instruments.py` | ✅ Yes — one file per market |

## Proof that the protocol is market-neutral

`tests/unit/test_adapter_protocol.py` has **24 tests** that prove it
via two demo adapters:

* `DemoSpotCryptoAdapter` — no derivatives, paginated instruments,
  24/7 UTC, 10 rps rate limit. Phase B/C are empty iterators.
  Phase D returns UTC bars with no open interest.

* `DemoPerpCryptoAdapter` — has derivatives BUT no expiries
  (`PERP` contract type), 20 rps rate limit. Phase B/C yield
  perpetual contracts with `expiry=None`. Phase D returns UTC bars
  with open interest.

Both satisfy `isinstance(adapter, MarketAdapter)` via `@runtime_checkable`.
If the protocol were leaky, one of these would fail.

## Backwards compatibility with PR #1's Groww adapter

The existing `brutex/adapters/groww/client.py` + `GrowwAdapter` +
`GrowwClientProtocol` stay **unchanged**. PR #2 only adds the new
market-neutral protocol alongside them. A follow-up commit will add
`brutex/adapters/groww/market.py` with a thin `GrowwMarketAdapter`
wrapper that translates the three semantic methods onto the existing
SDK-shaped calls. All the rate-limiting + dedup + audit plumbing
keeps working.

## What this guarantees

1. **Language freedom at the value layer.** Math, Greeks, indicators,
   and strategies live in Rust (`rust/fno-math/`). Adapters are Python
   control-plane code. The hot path is Rust. Python is just the glue.

2. **Common runtime.** Every market uses the same orchestrator, FSM,
   audit log, rate limiter, LMDB store, data lake, dashboard, Telegram,
   metrics. Zero duplication.

3. **Dynamic, scalable.** Add a market = one file. Add an indicator =
   one Rust file. Add a strategy = one Rust file. The brute-force
   backtest engine (P3+) runs the same combinatorial sweep over any
   market because the OHLCV stream shape is identical.

4. **Shared with tickvault (live trading).** `rust/fno-math/` is a
   Cargo path dependency consumed by both tickvault (live, via direct
   Rust) and BruteX (backtest, via PyO3 wheel). Writing a new indicator
   in Rust updates both products simultaneously with zero drift risk.

## What's NOT guaranteed (honest gap list)

1. **Per-adapter rate limit tuning** is on the operator. The protocol
   carries a rate limit RPS but the operator must set it right per
   their API tier.

2. **Symbol normalisation** is per-market. `NSE-NIFTY` in Groww is
   different from `NIFTY50` in Zerodha. If two adapters report the
   "same" symbol differently, cross-market strategies must handle it.

3. **Time zones** — every market converts to UTC on ingest, but the
   operator must remember that 2024-01-25 09:15 IST is 2024-01-25
   03:45 UTC and write queries accordingly.

4. **Option-chain completeness** — the protocol gives you an iterator
   of contracts, but whether the upstream market returns *every*
   strike at *every* expiry depends on the vendor's data quality.
   Adapters SHOULD log a gap report to the audit log if they detect
   missing strikes.
