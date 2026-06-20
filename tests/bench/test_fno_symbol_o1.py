"""O(1) bench for the F&O contract-symbol decode (DEC-FNO-CANDLE-LAKE-001).

:func:`brutex.data.fno_symbol.parse_fno_symbol` is the per-contract mapping the
``/db`` F&O candle browser runs once per discovered dataset (CLAUDE.md §2 r5 — O(1)
uniqueness / mapping). Its cost must be INDEPENDENT of how many contracts the lake
holds: there is no shared / accumulating state, so the per-symbol mean latency at a
large batch must not exceed the small-batch mean beyond the runner's ratio ceiling.

The gate is the standard ``mean_ns(N_big) / mean_ns(N_small) ≤ ceiling`` routed through
:func:`tests.bench._o1_tiers.ratio_ceiling` (robust on shared CI, strict 1.1× on the
M4-Pro tier). Honest envelope (Golden Rule #10): the parser is O(token-count) on a
BOUNDED-length symbol, hence O(1) per call by construction — this gate exists so a
future regression (e.g. an accidental O(n) scan over a global registry) fails CI
mechanically, not to re-prove constant-time string splitting.
"""

from __future__ import annotations

import time

from brutex.data.fno_symbol import parse_fno_symbol

from tests.bench._o1_tiers import ratio_ceiling

#: Small / big batch sizes — two orders of magnitude apart so an O(n) regression
#: would blow the ratio far past the ceiling.
_N_SMALL = 1_000
_N_BIG = 100_000


def _mean_ns_per_parse(n: int) -> float:
    """Mean nanoseconds to decode one contract symbol over *n* distinct symbols."""
    symbols = [f"NSE-NIFTY-30Jun25-{20000 + i}-CE" for i in range(n)]
    start = time.perf_counter_ns()
    for sym in symbols:
        parse_fno_symbol(sym)
    return (time.perf_counter_ns() - start) / n


def test_parse_fno_symbol_is_o1_per_contract() -> None:
    # Warm caches / interpreter, then measure — mirrors the sibling O(1) benches.
    _mean_ns_per_parse(_N_SMALL)
    _mean_ns_per_parse(_N_BIG)
    small = _mean_ns_per_parse(_N_SMALL)
    big = _mean_ns_per_parse(_N_BIG)
    ratio = big / small if small > 0 else 1.0
    ceiling = ratio_ceiling(5.0)
    assert ratio <= ceiling, (
        f"per-contract parse ratio {ratio:.2f} (big={big:.1f}ns small={small:.1f}ns) "
        f"exceeds the O(1) ceiling {ceiling:.2f} — a non-constant-time regression"
    )
