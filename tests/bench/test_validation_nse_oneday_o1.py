"""O(1) bench for the NSE/BSE 1-day cross-check hot pure paths (Golden Rule #5).

DEC-NSE-1DAY-VALIDATION-001. The two pure decision functions the verdict is
built from must be latency-independent of how many times they run:

* :func:`~brutex.data.validation.nse_oneday.compare_quotes` — a fixed 4-field
  Decimal comparison (O(1) by construction).
* :func:`~brutex.data.validation.nse_oneday.resolve_index_source` — two dict
  ``get``s on the default (no-override) path (O(1)).

Honest envelope (Golden Rule #10): these gates are per-call-cost stability
checks — the standard ``mean_ns(N_big) / mean_ns(N_small) <= ceiling`` ratio,
routed through :func:`tests.bench._o1_tiers.ratio_ceiling`. The CSV parse
(O(rows)) and the live network fetch (bounded latency, NOT O(1)-total) are
deliberately NOT gated here — this harness is an offline operator tool, never a
sweep hot path.
"""

from __future__ import annotations

import os
import time
from datetime import date
from decimal import Decimal

import pytest
from brutex.data.validation import nse_oneday as m
from brutex.data.validation.nse_oneday import OhlcQuote, compare_quotes, resolve_index_source

from tests.bench._o1_tiers import ratio_ceiling

pytestmark = pytest.mark.bench

#: Iteration tiers for the per-call-cost ratio (mirrors tests/bench/test_bar_lookup_o1.py
#: sizing — big enough to dwarf timer noise, small enough to stay fast on shared CI).
N_SMALL = 1_000
N_BIG = 100_000
#: Robust shared-CI ceiling (the strict 1.1× tier engages on the M4-Pro runner).
_SHARED_CI_CEILING = 5.0
_WARMUP = 256

_DATE = date(2025, 9, 24)


def _quote(source: str) -> OhlcQuote:
    return OhlcQuote(
        symbol="NIFTY",
        trade_date=_DATE,
        open=Decimal("25000.10"),
        high=Decimal("25100.20"),
        low=Decimal("24900.05"),
        close=Decimal("25050.75"),
        source=source,
    )


def _mean_ns(work: object, n: int) -> float:
    """Mean ns per call of zero-arg *work* over *n* iterations (with warm-up)."""
    fn = work  # type: ignore[assignment]
    for _ in range(min(_WARMUP, n)):
        fn()  # type: ignore[operator]
    t0 = time.perf_counter_ns()
    for _ in range(n):
        fn()  # type: ignore[operator]
    t1 = time.perf_counter_ns()
    return (t1 - t0) / n


def test_compare_quotes_is_o1() -> None:
    groww = _quote("groww")
    official = _quote("nse")

    def work() -> None:
        compare_quotes(groww, official)

    ratio = _mean_ns(work, N_BIG) / _mean_ns(work, N_SMALL)
    assert ratio <= ratio_ceiling(_SHARED_CI_CEILING), f"compare_quotes per-call drift {ratio:.2f}x"


def test_resolve_index_source_is_o1(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default (no-override) path — two dict gets, independent of call count.
    monkeypatch.delenv(m.INDEX_SOURCE_OVERRIDE_ENV, raising=False)
    assert os.environ.get(m.INDEX_SOURCE_OVERRIDE_ENV) is None

    def work() -> None:
        resolve_index_source("NSE-NIFTY")

    ratio = _mean_ns(work, N_BIG) / _mean_ns(work, N_SMALL)
    assert ratio <= ratio_ceiling(_SHARED_CI_CEILING), f"resolve drift {ratio:.2f}x"
