"""Bench pins for the Dhan pure laws (DEC-DHAN-ADAPTER-001).

Honest, narrow claims (Golden Rule #10):

* **parse is O(rows)** — per-row cost FLAT as the payload grows (a 10× payload costs
  ~10×, so cost-per-row big/small ≈ 1.0; gated at the shared :func:`ratio_ceiling`);
  parse is NEVER claimed O(1)-total.
* **index lookup is O(1)** — ``resolve_security_id`` / ``by_security_id`` hit time is
  FLAT vs index depth (100 rows vs 10,000 rows), the genuine O(1) dict-probe claim.
* **error classification is O(1)** — ``from_dhan_response`` cost is independent of the
  status/code (two dict probes + a couple of string ops).

Uses the repo's best-of-N-min idiom + the shared-CI ratio tiers
(:func:`tests.bench._o1_tiers.ratio_ceiling`).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from brutex.adapters.dhan.errors import from_dhan_response
from brutex.adapters.dhan.instruments import DhanInstrumentIndex, DhanScripRow
from brutex.adapters.dhan.parser import parse_candle_columns

from tests.bench._o1_tiers import ratio_ceiling


def _best_of(fn: Callable[[], object], *, rounds: int = 5) -> float:
    """Best-of-N wall time of *fn* (the house bench idiom — min removes scheduler noise)."""
    best = float("inf")
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - started)
    return best


def _payload(n: int) -> dict[str, object]:
    return {
        "open": [100.0 + i * 0.05 for i in range(n)],
        "high": [101.0 + i * 0.05 for i in range(n)],
        "low": [99.0 + i * 0.05 for i in range(n)],
        "close": [100.5 + i * 0.05 for i in range(n)],
        "volume": [1000 + i for i in range(n)],
        "timestamp": [1_700_000_000 + 60 * i for i in range(n)],
    }


def test_parse_cost_per_row_is_flat() -> None:
    """O(rows) honesty: per-ROW parse cost stays flat as the payload grows 10×."""
    small, big = _payload(200), _payload(2000)
    parse_candle_columns(small)  # warm-up (imports, Decimal contexts)
    t_small = _best_of(lambda: parse_candle_columns(small))
    t_big = _best_of(lambda: parse_candle_columns(big))
    per_row_small = t_small / 200
    per_row_big = t_big / 2000
    assert per_row_big <= per_row_small * ratio_ceiling(8.0)


def _index(n: int) -> DhanInstrumentIndex:
    rows = [
        DhanScripRow(
            security_id=i,
            trading_symbol=f"SYM{i}",
            exchange_segment="NSE_EQ",
            instrument_type="EQUITY",
            series=None,
            expiry_date=None,
            option_type=None,
            strike_price=None,
            lot_size=None,
            tick_size=None,
        )
        for i in range(n)
    ]
    return DhanInstrumentIndex.build(rows)


def test_index_lookup_is_o1_vs_depth() -> None:
    """The dict-probe lookup is FLAT at 100× the index depth (the genuine O(1) claim)."""
    small, big = _index(100), _index(10_000)

    def probe(index: DhanInstrumentIndex, n: int) -> None:
        for i in range(1000):
            index.resolve_security_id("NSE_EQ", f"SYM{i % n}")

    probe(small, 100)  # warm-up
    t_small = _best_of(lambda: probe(small, 100))
    t_big = _best_of(lambda: probe(big, 10_000))
    assert t_big <= t_small * ratio_ceiling(8.0)


def test_error_classification_is_o1() -> None:
    """Classification cost is independent of the (status, code) pair — flat."""

    def classify_batch(status: int, code: str | None) -> None:
        body: dict[str, object] = {} if code is None else {"errorCode": code}
        for _ in range(2000):
            from_dhan_response(status, body)

    classify_batch(429, None)  # warm-up
    t_status = _best_of(lambda: classify_batch(429, None))
    t_code = _best_of(lambda: classify_batch(500, "DH-901"))
    t_unknown = _best_of(lambda: classify_batch(418, "DH-999"))
    ceiling = ratio_ceiling(8.0)
    assert t_code <= t_status * ceiling
    assert t_unknown <= t_status * ceiling
