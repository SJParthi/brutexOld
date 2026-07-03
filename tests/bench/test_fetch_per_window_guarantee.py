"""The fetch-per-window gate — measured, PRODUCTION-shaped, honest (two pinned quantities).

DEC-FETCH-PER-WINDOW-GUARANTEE-001 (MIGRATION.md #1026) shipped this gate replaying a chain
whose windows fit ONE 30-day vendor chunk, and asserted ``fpw ≤ 1.2`` + "8h fits". A verified
forensic audit of a real production pull then measured **2.59 fetches per (contract, interval)
window** while this gate read ``1.198`` and PASSED — because the production DEFAULT windows do
NOT fit one chunk: an option's default span is ``expiry − 30d → expiry + 1d`` = **31 days → 2 ×
30-day chunks** (DEC-FNO-LOOKBACK-SPLIT-001 / DEC-FNO-FAR-EXPIRY-WINDOW-001, MIGRATION.md #894 /
#1127) and a future's is ``expiry − 60d → expiry + 1d`` = **61 days → 3 chunks**
(DEC-FUTURES-LOOKBACK-60D-001), per the 30-day 1-minute tier of
``docs/groww/api-reference/03-backtesting.md`` (:mod:`brutex.adapters.groww.chunks`). The gate
measured only the EMPTY-retry amplification and was blind to the structural chunk tiling, so
its "8h feasibility" arithmetic did not hold for production
(DEC-FETCH-PER-WINDOW-GATE-HONESTY-001 — this rewrite).

This module therefore pins TWO distinct quantities, never conflating them again:

1. **The single-chunk LAB quantity** (:func:`test_empty_retry_bounding_single_chunk_pin`) —
   window-level fetch amplification from the in-pull EMPTY-retry alone, replayed over windows
   that tile into ONE chunk. This is the quantity #1026's adaptive strike-chain bounding
   (:mod:`brutex.adapters.groww.strike_chain_bound`) actually drives; it stays independently
   pinned at its own ceiling (``≤ 1.2``) so the empty-retry lever can never silently regress.
   :func:`test_baseline_without_bounding_exceeds_ceiling` proves that pin has teeth.

2. **The PRODUCTION-shaped quantity** (:func:`test_production_shaped_fetch_per_window_gate`) —
   the fetches-per-window a real pull actually pays. The replay derives every contract's window
   from the REAL default-window law (:func:`~brutex.adapters.groww.contract_bounds.
   default_fno_minute_window`, exactly as ``historical_pull._resolve_minute_windows`` does) and
   runs it through the REAL ingest spine, so the measured ratio reflects chunk tiling × empty
   retries as production experiences them. The ceiling is DERIVED from the replayed chain, not
   chosen: each option window structurally costs ``chunks_per_option_window`` (2, derived from
   the real chunk planner over the real default span) fetches per pass, and the mean passes per
   option window is bounded by the single-chunk pin (1.2); each (liquid) future window costs its
   own structural chunks (3) once. For this chain (162 options + 1 future) that derives to
   ``(162·2·1.2 + 3) / 163 ≈ 2.404``, and the replay measures ``≈ 2.399`` — consistent with the
   production forensic audit's 2.59 (which additionally carries live transient retries + a
   different liquid/dead mix, neither replayed here).

**The honest 8-hour arithmetic (Golden Rule #10 — never fake feasibility).** At the worst-case
sustained ``3 r/s`` (the AIMD soft floor, CLAUDE.md §2 Rule 6), ``3 × 3600 × 8h = 86,400``
vendor fetches fit in 8h, so 8h covers ``86,400 / fpw ≈ 36,000`` windows at the honest
production-shaped rate — BELOW the 48,145-window production scope the #1026 forensic audit
measured (122,243 fetches / 48,145 windows). The TRUE achievable wall-clock for that audited
scope is ``48,145 × fpw / 3 r/s ≈ 10.7 h``, NOT 8h. The gate ASSERTS this infeasibility
(``projected_8h < 48,145``) so the ledger claim can never silently go stale in EITHER
direction: if a future change genuinely cuts the structural cost (a wider vendor chunk, a lake
manifest, a shorter default span), this pin FAILS LOUDLY and forces the ceiling + the
MIGRATION.md feasibility claim to be consciously re-derived — exactly the forcing function the
stale "1.2 / fits 8h" claim lacked.

Honest scope: a CONTROLLED deterministic replay over a representative chain (bulk deep-OTM +
empty, a narrow liquid band, one liquid future); it measures OUR pipeline's structural +
empty-retry fetch cost, NOT live vendor 429 storms (those ride the separate transient ladder).
The 48,145-window reference scope is the PRE-#1127 audited pull; DEC-FNO-FAR-EXPIRY-WINDOW-001
has since removed the not-yet-open far contracts from a pull's scope, so the CURRENT production
window count per pull is smaller by an unmeasured amount (a fresh production audit would be
needed to claim a number — not claimed here). The 8h projection assumes the controller
sustains 3 r/s, which it provably can (CLAUDE.md §2 Rule 6); it is a wall-clock LOWER bound,
not a claim about vendor health.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta

from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker
from brutex.adapters.groww.chunks import plan_candle_windows
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contract_bounds import default_fno_minute_window
from brutex.adapters.groww.option_ingest import run_groww_option_ingest
from brutex.adapters.groww.strike_chain_bound import (
    DEFAULT_DEEP_EMPTY_RETRY,
    DEFAULT_STRIKE_BAND,
    StrikeChainBudget,
    build_empty_retry_budget,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.calendars.nse import is_trading_day
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult

#: The operator's worst-case sustained rate (CLAUDE.md §6 — the AIMD soft floor).
_WORST_CASE_RPS: float = 3.0
#: The 8h fetch budget at the worst-case rate.
_BUDGET_8H: int = int(_WORST_CASE_RPS * 3600 * 8)  # 86,400
#: The SINGLE-CHUNK ceiling — the #1026 empty-retry-bounding pin (per-window PASSES, since a
#: single-chunk window costs exactly one fetch per pass). NOT a production feasibility claim.
_FPW_CEILING_SINGLE_CHUNK: float = 1.2
#: The audited production scope: 48,145 ``(contract, interval)`` windows in the #1026 forensic
#: audit's real pull (122,243 fetches / 48,145 windows = 2.54 fpw). The PRE-#1127 scope — the
#: reference the honest 8h arithmetic is stated against (see the module docstring).
_PRODUCTION_SCOPE_WINDOWS: int = 48_145

#: The replayed chain's single shared expiry (a fully-expired past date, so every default
#: window is OPEN under the strict window law — the not-yet-open skip can never fire here).
_EXPIRY = date(2025, 1, 30)
#: The pull envelope for the PRODUCTION-shaped replay — wide enough that the per-type default
#: windows (option ``expiry − 30d``, future ``expiry − 60d``) are never clamped by it.
_FULL_START = date(2024, 10, 1)
_FULL_END = date(2025, 2, 1)
#: The only interval Track 1 fetches (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) — 30-day chunk tier.
_INTERVAL = "1minute"


def _now() -> datetime:
    """A frozen IST clock so every window is firmly in the past (no current-day clamp games)."""
    return datetime(2026, 6, 13, 8, 0, tzinfo=UTC)


def _build_chain(
    *, n_strikes_per_side: int, include_future: bool = False
) -> list[DerivativeContract]:
    """A representative NIFTY chain: ``n_strikes_per_side`` strikes each side of ATM, CE+PE.

    The center strike is ATM. Which strikes are LIQUID (return a real bar) vs deep-OTM DEAD
    (return a clean empty ``200``) is decided at fetch time by :func:`_liquid_symbols` /
    :class:`_CountingSDK` — mirroring a real options chain: a wide listed strike grid, a narrow
    liquid band, the bulk deep + empty. ``include_future=True`` appends the underlying's single
    serial-month FUT leg (always liquid — a front future trades continuously), giving the
    PRODUCTION-shaped replay its real 60-day-window future alongside the 30-day-window options.
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
                    expiry=_EXPIRY,
                    symbol=f"NSE-NIFTY-30Jan25-{strike}-{opt_type}",
                    contract_type=opt_type,
                    strike=float(strike),
                )
            )
    if include_future:
        contracts.append(
            DerivativeContract(
                underlying_symbol="NIFTY",
                expiry=_EXPIRY,
                symbol="NSE-NIFTY-30Jan25-FUT",
                contract_type="FUT",
                strike=None,
            )
        )
    return contracts


def _liquid_symbols(
    contracts: Sequence[DerivativeContract], *, liquid_half_width: int
) -> frozenset[str]:
    """The symbols that return bars: strikes within ``liquid_half_width`` of ATM + every FUT.

    A strike-less contract (a future) is always liquid — a serial-month future trades
    continuously over its default window, unlike the chain's deep-OTM strikes.
    """
    center = 23000.0
    step = 50.0
    return frozenset(
        c.symbol
        for c in contracts
        if c.strike is None or abs(c.strike - center) <= liquid_half_width * step
    )


def _hermetic_budget(contracts: Sequence[DerivativeContract]) -> StrikeChainBudget:
    """Build the empty-retry budget with the band/deep defaults passed EXPLICITLY.

    Hermeticity (hostile-review polish): ``build_empty_retry_budget`` resolves ``band`` /
    ``deep_attempts`` from ``BRUTEX_FNO_STRIKE_BAND`` / ``BRUTEX_FNO_DEEP_EMPTY_RETRY`` when
    they are ``None``, while this gate's ceilings hardcode the 1.2 pass allowance and the
    measured margins are deliberately razor-thin (2.3988 vs 2.4037). Passing the audited
    defaults explicitly pins the replay to the shipped configuration, so an operator env var
    set in the CI shell can never spuriously fail (or flatter) the gate.
    """
    return build_empty_retry_budget(
        contracts, band=DEFAULT_STRIKE_BAND, deep_attempts=DEFAULT_DEEP_EMPTY_RETRY
    )


def _production_windows(
    contracts: Sequence[DerivativeContract],
) -> dict[str, tuple[date, date]]:
    """Each contract's REAL production default window — via the real window law, never inlined.

    Mirrors ``historical_pull._resolve_minute_windows`` exactly: option ``expiry − 30d →
    expiry + 1d``, future ``expiry − 60d → expiry + 1d``, clamped into the envelope
    (:func:`default_fno_minute_window`). Every replay expiry is in the past, so the strict
    window law's not-yet-open ``None`` (DEC-FNO-FAR-EXPIRY-WINDOW-001) can never occur here —
    asserted, so a law change that starts skipping these contracts fails loudly.
    """
    windows: dict[str, tuple[date, date]] = {}
    for contract in contracts:
        window = default_fno_minute_window(
            contract.expiry,
            full_start=_FULL_START,
            full_end=_FULL_END,
            is_option=contract.contract_type in {"CE", "PE"},
        )
        assert window is not None, f"replay window unexpectedly not open: {contract.symbol}"
        windows[contract.symbol] = window
    return windows


def _chunks_for(window: tuple[date, date]) -> int:
    """The structural vendor-chunk count of *window* — via the REAL chunk planner (30d tier)."""
    start, end = window
    return len(
        plan_candle_windows(
            datetime.combine(start, time()), datetime.combine(end, time()), interval=_INTERVAL
        )
    )


class _CountingSDK:
    """A ``GrowwSDK``-shaped fake that COUNTS fetches; liquid symbols return a bar, dead empty.

    Each liquid chunk fetch returns ONE bar stamped 09:30 IST on the FIRST NSE trading day
    on/after its chunk's start (the calendar-aware ingest-boundary session filter,
    DEC-INGEST-BOUNDARY-INTEGRITY-001, keeps it — a raw chunk-start day can be a weekend /
    holiday, e.g. the FUT window's ``expiry − 60d`` = Sunday 2024-12-01, and would be
    honestly dropped as ``non_trading_date``). Chunks tile ~30 days apart while the
    trading-day walk advances at most a few days, so a multi-chunk liquid window still
    stores one DISTINCT bar per chunk — keeping the no-data-lost assertion exact under
    production-shaped chunk tiling.
    """

    def __init__(self, liquid: frozenset[str]) -> None:
        self._liquid = liquid
        self.fetches = 0

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        self.fetches += 1
        symbol = str(kw["groww_symbol"])
        if symbol in self._liquid:
            bar_day = date.fromisoformat(str(kw["start_time"])[:10])
            while not is_trading_day(bar_day):
                bar_day += timedelta(days=1)
            return {
                "candles": [
                    [
                        f"{bar_day.isoformat()}T09:30:00",
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
    contract_windows: Mapping[str, tuple[date, date]] | None = None,
    start: date = date(2025, 1, 1),
    end: date = date(2025, 1, 31),
) -> tuple[int, int, int]:
    """Run ONE full ingest pass; return ``(total_fetches, total_windows, bars_stored)``.

    A window = one ``(contract, interval)`` pair. The serial default runner is used so the
    fetch count is deterministic (the rate pool adds NO fetches — CLAUDE.md §2 Rule 6). The
    default ``start``/``end`` (a 30-day span → ONE chunk) is the single-chunk LAB shape;
    the PRODUCTION-shaped tests pass the envelope + the real per-contract *contract_windows*.
    """
    sdk = _CountingSDK(_liquid_symbols(contracts, liquid_half_width=8))
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)
    sink = _CountingSink()
    report = run_groww_option_ingest(
        client,
        sink,
        contracts=list(contracts),
        intervals=(_INTERVAL,),
        start=start,
        end=end,
        now_ist=_now,
        contract_windows=contract_windows,
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


def test_empty_retry_bounding_single_chunk_pin() -> None:
    """The #1026 lever stays pinned: single-chunk fetches-per-window ≤ 1.2 (a LAB quantity).

    Replays a representative 40-strike-per-side NIFTY chain (the bulk deep-OTM + empty) over a
    30-day range that tiles into ONE vendor chunk, WITH the per-contract empty-retry budget.
    With one chunk per window, fetches == empty-retry PASSES, so this pins the adaptive
    strike-chain bounding at its own ceiling INDEPENDENTLY of chunk tiling: a regression that
    re-inflated the empty-retry on dead strikes (or removed the bounding) fails here. This is
    NOT the production fetches-per-window — production windows span 2+ chunks; the honest
    production quantity + the 8h arithmetic live in
    :func:`test_production_shaped_fetch_per_window_gate`.
    """
    contracts = _build_chain(n_strikes_per_side=40)
    budget = _hermetic_budget(contracts)
    fetches, windows, bars = _run(contracts, empty_retry_by_symbol=dict(budget.by_symbol) or None)

    fpw = fetches / windows
    assert fpw <= _FPW_CEILING_SINGLE_CHUNK, (
        f"single-chunk fetches-per-window {fpw:.3f} exceeds the {_FPW_CEILING_SINGLE_CHUNK} "
        f"pin ({fetches} fetches / {windows} windows) — the empty-retry bounding regressed"
    )
    # No data lost: every liquid strike's bar is stored (one chunk → one bar each).
    assert bars == len(_liquid_symbols(contracts, liquid_half_width=8))


def test_baseline_without_bounding_exceeds_ceiling() -> None:
    """The single-chunk pin has TEETH: the SAME chain WITHOUT bounding is ABOVE 1.2.

    Proves the empty-retry guarantee is real — the pre-#1026 full-3×-empty-retry pull would
    FAIL the pin (deep-OTM strikes pay 3 fetches each), so the WITH-budget pass is what makes
    it fit. Also asserts the WITH-budget pass stores the SAME bars as the baseline (no data
    lost).
    """
    contracts = _build_chain(n_strikes_per_side=40)

    base_fetches, base_windows, base_bars = _run(contracts, empty_retry_by_symbol=None)
    base_fpw = base_fetches / base_windows
    assert base_fpw > _FPW_CEILING_SINGLE_CHUNK, (
        f"baseline fetches-per-window {base_fpw:.3f} is unexpectedly within ceiling — "
        "the chain is not representative (the gate would not have teeth)"
    )

    budget = _hermetic_budget(contracts)
    _bounded_fetches, _bounded_windows, bounded_bars = _run(
        contracts, empty_retry_by_symbol=dict(budget.by_symbol) or None
    )
    # Same bars stored: the bounding declines empty re-confirmations only, never a real bar.
    assert bounded_bars == base_bars > 0


def test_production_shaped_fetch_per_window_gate() -> None:
    """THE GATE: production-shaped fetches-per-window ≤ the derived ceiling; 8h honesty pinned.

    Replays the representative chain (162 options + 1 future) with every window derived from
    the REAL default-window law (option 31-day span → 2 chunks; future 61-day span → 3 chunks,
    both via the real planner — nothing inlined), through the real spine with the adaptive
    empty-retry budget. Asserts:

    1. **Structural honesty (anti-flattering pin):** every option window tiles into ≥ 2 vendor
       chunks — the gate can never silently degrade back to the single-chunk LAB shape that
       flattered production (1.198 measured vs 2.59 audited). If the real window law ever
       shrinks the default span under one chunk, this fails loudly and the ceiling + the
       MIGRATION.md claim must be consciously re-derived.
    2. **The derived ceiling:** ``fpw ≤ (n_opt · opt_chunks · 1.2 + Σ fut_chunks) / n_windows``
       — per option window, the structural chunk count times the single-chunk pass allowance
       the empty-retry pin bounds (1.2); per (liquid) future window, its structural chunks
       once. For this chain: ``(162·2·1.2 + 3) / 163 ≈ 2.404``; measured ``≈ 2.399``. Also
       ``fpw ≥`` the pure structural floor (every window pays its chunks at least once), so
       the measurement itself is sane.
    3. **8h honesty (Golden Rule #10):** at 3 r/s the 86,400-fetch 8h budget covers
       ``86,400 / fpw ≈ 36,000`` windows — asserted STRICTLY BELOW the audited 48,145-window
       production scope (#1026's forensic audit). 8h does NOT fit that scope at the honest
       rate; the true achievable wall-clock is ``48,145 × fpw / 3 r/s ≈ 10.7 h`` (in the
       failure message). If this pin ever fails because the fetch cost genuinely DROPPED,
       that is the intended forcing function: update the pins + the ledger to the new truth
       (never leave a stale infeasibility claim), exactly as this fix did for the stale
       "1.2 / fits 8h" claim.
    4. **No data lost:** every liquid window stores one bar per chunk (34 options × 2 + 3).
    """
    contracts = _build_chain(n_strikes_per_side=40, include_future=True)
    windows_map = _production_windows(contracts)
    structural = {symbol: _chunks_for(window) for symbol, window in windows_map.items()}
    option_symbols = [c.symbol for c in contracts if c.contract_type in {"CE", "PE"}]
    future_symbols = [c.symbol for c in contracts if c.contract_type == "FUT"]
    n_windows = len(contracts)

    # (1) Structural honesty: the replay is genuinely multi-chunk (the production shape).
    option_chunks = {structural[s] for s in option_symbols}
    assert min(option_chunks) >= 2, (
        f"option windows tile into {sorted(option_chunks)} chunk(s) — the replay degraded to "
        "the single-chunk LAB shape; re-derive the ceiling + the MIGRATION.md claim"
    )

    budget = _hermetic_budget(contracts)
    fetches, windows, bars = _run(
        contracts,
        empty_retry_by_symbol=dict(budget.by_symbol) or None,
        contract_windows=windows_map,
        start=_FULL_START,
        end=_FULL_END,
    )
    assert windows == n_windows
    fpw = fetches / windows

    # (2) The derived ceiling: structural chunks × the pinned per-window pass allowance for
    # options, plus each liquid future's structural chunks once — derived from the replayed
    # chain, never a hand-picked constant (and never the flattering single-chunk 1.2).
    #
    # EXACTNESS PRECONDITION (hostile-review polish): weighting the summed option chunks by the
    # flat 1.2 pass allowance is exact ONLY because the option-chunk count is UNIFORM here (all
    # options share the one expiry `_EXPIRY` → identically 2 chunks, asserted in (1)); with a
    # uniform chunk count, Σ(chunks·passes) ≤ chunks · Σpasses ≤ chunks · 1.2 · n_opt factors
    # cleanly. If this chain ever mixes expiries (non-uniform chunk counts), passes and chunks
    # correlate per contract and the flat weighting must be re-derived per chunk-count group
    # (e.g. Σ over groups of chunks_g · 1.2 · n_g), not assumed.
    derived_ceiling = (
        sum(structural[s] for s in option_symbols) * _FPW_CEILING_SINGLE_CHUNK
        + sum(structural[s] for s in future_symbols)
    ) / n_windows
    structural_floor = sum(structural.values()) / n_windows
    assert structural_floor <= fpw <= derived_ceiling, (
        f"production-shaped fetches-per-window {fpw:.3f} outside "
        f"[{structural_floor:.3f}, {derived_ceiling:.3f}] "
        f"({fetches} fetches / {windows} windows) — chunk tiling or empty-retry regressed"
    )

    # (3) The honest 8h arithmetic — assert the TRUE achievable number, never fake feasibility.
    projected_8h_windows = _BUDGET_8H / fpw
    true_hours = _PRODUCTION_SCOPE_WINDOWS * fpw / _WORST_CASE_RPS / 3600.0
    assert projected_8h_windows < _PRODUCTION_SCOPE_WINDOWS, (
        f"8h @ 3 r/s now covers {projected_8h_windows:.0f} windows ≥ the audited "
        f"{_PRODUCTION_SCOPE_WINDOWS}-window scope (true wall-clock {true_hours:.1f} h) — "
        "feasibility flipped: re-derive this pin + update the MIGRATION.md honesty claim"
    )

    # (4) No data lost: one distinct bar per chunk of every liquid window.
    liquid = _liquid_symbols(contracts, liquid_half_width=8)
    assert bars == sum(structural[s] for s in liquid)
