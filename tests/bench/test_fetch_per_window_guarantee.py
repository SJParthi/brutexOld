"""The fetch-per-window GUARANTEE — a measured CI gate (DEC-FETCH-PER-WINDOW-GUARANTEE-001).

The operator's criterion: at a worst-case ``3 r/s``, a full F&O option-chain pull must
cover its target windows within ``~8 hours`` INCLUDING the rate-limiter retry overhead.
The arithmetic: ``3 r/s × 3600 × 8h = 86,400`` vendor fetches available in 8h, so the pull
fits ONLY IF the fetches-per-window ratio is ``≤ ~1.2``.

A measured baseline pull spent ``~2.54`` fetches per ``(contract, interval)`` window —
almost entirely the in-pull EMPTY-retry budget (``3``) firing on every clean-but-empty deep
out-of-the-money strike that never traded. DEC-FETCH-PER-WINDOW-GUARANTEE-001's adaptive
strike-chain bounding (:mod:`brutex.adapters.groww.strike_chain_bound`) gives a confirmed-deep
strike a REDUCED empty-retry budget (``1``), so a dead strike costs ONE fetch to confirm empty
instead of three — driving the ratio toward ``~1.0`` WITHOUT losing data (the first non-empty
fetch always wins, so a deep strike that DOES return bars is stored exactly as before).

This module is the GATE: it REPLAYS a representative NIFTY chain end-to-end through the REAL
:func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest` spine against a fetch-
COUNTING fake SDK, measures the real fetches-per-window WITH the budget, and ASSERTS

* fetches-per-window ``≤`` :data:`_FPW_CEILING` (the ``≤ 1.2`` criterion), AND
* the projected 8h coverage (``86,400 / fetches-per-window``) ``≥`` the chain's window count,

so a regression that re-inflated the empty-retry on dead strikes — or removed the bounding —
would FAIL CI. It also asserts the BASELINE (no budget) is ABOVE the ceiling, proving the gate
has teeth (it would not pass without the fix). NO data is lost is proven separately by the unit
suite; here we additionally assert the WITH-budget pull stores the SAME bars as the baseline.

Honest scope (Golden Rule #10): this is a CONTROLLED replay over a representative chain whose
liquid/dead split mirrors a real options chain (the bulk of strikes deep-OTM + empty); it
measures the OUR-pipeline amplification (the empty-retry), NOT live vendor 429 storms (those
ride the separate, untouched transient ladder). The 8h projection is ``total_fetches / 3 r/s``
— a wall-clock LOWER bound assuming the controller sustains 3 r/s, which it provably can
(CLAUDE.md §2 Rule 6); it is NOT a claim that every real pull finishes in 8h regardless of
vendor health.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.option_ingest import run_groww_option_ingest
from brutex.adapters.groww.strike_chain_bound import build_empty_retry_budget
from brutex.adapters.protocol import DerivativeContract
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult

#: The operator's worst-case sustained rate (CLAUDE.md §6 — the AIMD soft floor).
_WORST_CASE_RPS: float = 3.0
#: The 8h fetch budget at the worst-case rate.
_BUDGET_8H: int = int(_WORST_CASE_RPS * 3600 * 8)  # 86,400
#: The fetches-per-window ceiling the guarantee enforces (the ``≤ 1.2`` criterion).
_FPW_CEILING: float = 1.2


def _now() -> datetime:
    """A frozen IST clock so every window is firmly in the past (no current-day clamp games)."""
    return datetime(2026, 6, 13, 8, 0, tzinfo=UTC)


def _build_chain(*, n_strikes_per_side: int) -> list[DerivativeContract]:
    """A representative NIFTY chain: ``n_strikes_per_side`` strikes each side of ATM, CE+PE.

    The center strike is ATM. Which strikes are LIQUID (return a real bar) vs deep-OTM DEAD
    (return a clean empty ``200``) is decided at fetch time by :func:`_liquid_symbols` /
    :class:`_CountingSDK` — mirroring a real options chain: a wide listed strike grid, a narrow
    liquid band, the bulk deep + empty.
    """
    center = 23000
    step = 50
    contracts: list[DerivativeContract] = []
    for offset in range(-n_strikes_per_side, n_strikes_per_side + 1):
        strike = center + offset * step
        for opt_type in ("CE", "PE"):
            contracts.append(
                DerivativeContract(
                    underlying_symbol="NIFTY",
                    expiry=date(2025, 1, 30),
                    symbol=f"NSE-NIFTY-30Jan25-{strike}-{opt_type}",
                    contract_type=opt_type,
                    strike=float(strike),
                )
            )
    return contracts


def _liquid_symbols(
    contracts: Sequence[DerivativeContract], *, liquid_half_width: int
) -> frozenset[str]:
    """The symbols of the strikes within ``liquid_half_width`` of the ATM center (return bars)."""
    center = 23000.0
    step = 50.0
    return frozenset(
        c.symbol
        for c in contracts
        if c.strike is not None and abs(c.strike - center) <= liquid_half_width * step
    )


class _CountingSDK:
    """A ``GrowwSDK``-shaped fake that COUNTS fetches; liquid symbols return a bar, dead empty."""

    def __init__(self, liquid: frozenset[str]) -> None:
        self._liquid = liquid
        self.fetches = 0

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        self.fetches += 1
        symbol = str(kw["groww_symbol"])
        if symbol in self._liquid:
            return {
                "candles": [
                    [
                        "2025-01-02T09:30:00",
                        100.0,
                        101.0,
                        99.0,
                        100.5,
                        1000,
                        5000,
                    ]
                ]
            }
        return {"candles": []}  # a dead deep-OTM strike — clean empty 200

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover - unused
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover - unused
        return {"contracts": []}


class _CountingSink:
    """Records the total bars stored (to prove the WITH-budget pull loses no data)."""

    def __init__(self) -> None:
        self.bars_stored = 0

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, merge_mode
        self.bars_stored += len(bars)
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _run(
    contracts: Sequence[DerivativeContract],
    *,
    empty_retry_by_symbol: Mapping[str, int] | None,
) -> tuple[int, int, int]:
    """Run ONE full ingest pass; return ``(total_fetches, total_windows, bars_stored)``.

    A window = one ``(contract, interval)`` pair. The serial default runner is used so the
    fetch count is deterministic (the rate pool adds NO fetches — CLAUDE.md §2 Rule 6).
    """
    sdk = _CountingSDK(_liquid_symbols(contracts, liquid_half_width=8))
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)
    sink = _CountingSink()
    report = run_groww_option_ingest(
        client,
        sink,
        contracts=list(contracts),
        intervals=("1minute",),
        start=date(2025, 1, 1),
        end=date(2025, 1, 31),
        now_ist=_now,
        empty_retry_by_symbol=empty_retry_by_symbol,
        # Stand the mass-empty circuit breaker DOWN for this replay (DEC-EMPTY-WINDOW-
        # CIRCUIT-BREAKER-001): the deliberately deep-OTM-heavy synthetic chain — above
        # all the un-bounded BASELINE arm, which passes no deep-strike budget and so has
        # no expected-empty exclusion — is EXACTLY the mass-empty shape the production
        # breaker now HALTS (its own trip/halt behaviour is pinned in
        # test_groww_option_ingest.py). This bench measures the per-window FETCH COST,
        # not settle safety, so the guard is disabled here deliberately.
        empty_breaker=EmptySettleBreaker(enabled=False),
    )
    return sdk.fetches, report.windows, sink.bars_stored


def test_fetch_per_window_guarantee_8h_coverage() -> None:
    """WITH adaptive bounding: fetches-per-window ≤ 1.2 AND 8h covers the chain — the GATE.

    Replays a representative 41-strike-per-side NIFTY chain (the bulk deep-OTM + empty)
    through the real ingest spine WITH the per-contract empty-retry budget, then asserts the
    operator's criterion: fetches-per-window ``≤ 1.2`` AND the projected 8h coverage at 3 r/s
    (``86,400 / fpw``) is ``≥`` the chain's window count. A regression that re-inflated the
    empty-retry would push the ratio above 1.2 and FAIL here.
    """
    contracts = _build_chain(n_strikes_per_side=40)
    budget = build_empty_retry_budget(contracts)
    fetches, windows, bars = _run(contracts, empty_retry_by_symbol=dict(budget.by_symbol) or None)

    fpw = fetches / windows
    projected_8h_windows = _BUDGET_8H / fpw

    # (1) The fetches-per-window criterion.
    assert fpw <= _FPW_CEILING, (
        f"fetches-per-window {fpw:.3f} exceeds the {_FPW_CEILING} guarantee ceiling "
        f"({fetches} fetches / {windows} windows) — the empty-retry bounding regressed"
    )
    # (2) The 8h-coverage criterion: at 3 r/s the 8h budget covers AT LEAST this chain's windows.
    assert projected_8h_windows >= windows, (
        f"8h @ 3 r/s covers only {projected_8h_windows:.0f} windows at {fpw:.3f} fpw, "
        f"below the chain's {windows} windows"
    )
    # (3) No data lost: every liquid strike's bar is stored.
    assert bars == len(_liquid_symbols(contracts, liquid_half_width=8))


def test_baseline_without_bounding_exceeds_ceiling() -> None:
    """The gate has TEETH: the SAME chain WITHOUT bounding is ABOVE the 1.2 ceiling.

    Proves the guarantee is real — the pre-feature full-3×-empty-retry pull would FAIL the
    criterion (deep-OTM strikes pay 3 fetches each), so the WITH-budget pass is what makes it
    fit. Also asserts the WITH-budget pass stores the SAME bars as the baseline (no data lost).
    """
    contracts = _build_chain(n_strikes_per_side=40)

    base_fetches, base_windows, base_bars = _run(contracts, empty_retry_by_symbol=None)
    base_fpw = base_fetches / base_windows
    assert base_fpw > _FPW_CEILING, (
        f"baseline fetches-per-window {base_fpw:.3f} is unexpectedly within ceiling — "
        "the chain is not representative (the gate would not have teeth)"
    )

    budget = build_empty_retry_budget(contracts)
    _bounded_fetches, _bounded_windows, bounded_bars = _run(
        contracts, empty_retry_by_symbol=dict(budget.by_symbol) or None
    )
    # Same bars stored: the bounding declines empty re-confirmations only, never a real bar.
    assert bounded_bars == base_bars > 0
