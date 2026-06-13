"""O(1) benchmark for the Groww contract-symbol parser (DEC-GROWW-OPTION-INGEST-001).

CLAUDE.md §2 r5 / §5: every hot primitive must be O(1) per call —
``p99(N_big)/p99(N_small) ≤ 1.1×`` (strict M4-Pro tier) / robust ``< 5×`` on
shared CI. The option lake pull parses ONE
:func:`~brutex.adapters.groww.contracts.parse_contract_symbol` per discovered
contract (potentially millions across the universe × expiries × strikes), so the
parser is a hot path: a regression introducing growing per-call state (an
unbounded cache keyed by symbol) would blow the ratio and fail the gate.

Mirrors the ``_o1_tiers`` two-tier pattern (``tests/bench/test_blake3_combo_hash_o1.py``).
"""

from __future__ import annotations

import time
from datetime import date
from typing import Final

from brutex.adapters.groww.contracts import parse_contract_symbol

from tests.bench._o1_tiers import ratio_ceiling

_SMALL: Final[int] = 1_000
_LARGE: Final[int] = 100_000
_EXPIRY: Final[date] = date(2025, 1, 2)


def _parse_once(i: int) -> str:
    """Parse one canonical option symbol (vary only the strike)."""
    strike = 20_000 + (i & 0xFFFF) * 50
    c = parse_contract_symbol(
        f"NSE-NIFTY-02Jan25-{strike}-CE", underlying_symbol="NIFTY", expiry=_EXPIRY
    )
    return c.symbol


class TestContractParseIsO1:
    """``parse_contract_symbol`` is O(1) per call — pure, no growing state."""

    def test_parse_is_correct(self) -> None:
        c = parse_contract_symbol(
            "NSE-NIFTY-02Jan25-24650-CE", underlying_symbol="NIFTY", expiry=_EXPIRY
        )
        assert c.contract_type == "CE" and c.strike == 24650.0

    def test_parse_at_small_scale(self, benchmark) -> None:
        assert benchmark(lambda: _parse_once(7)).endswith("-CE")

    def test_parse_at_large_scale(self, benchmark) -> None:
        assert benchmark(lambda: _parse_once(123_457)).endswith("-CE")

    def test_parse_ratio_below_5x(self) -> None:
        """Per-call latency over a 100k batch must match a 1k batch (< 5×)."""

        def _time(n: int) -> float:
            for i in range(50):  # warm
                _ = _parse_once(i)
            t0 = time.perf_counter_ns()
            for i in range(n):
                _ = _parse_once(i)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"parse_contract_symbol per-call latency ratio {ratio:.2f}× (100k vs 1k) "
            f"exceeds 5× — the parser may have acquired growing per-call state. "
            f"t_1k={t_small:.1f}ns, t_100k={t_large:.1f}ns."
        )
