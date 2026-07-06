"""In-package Phase-0 Groww probe — the UI / source-of-truth live checks.

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009 §4). ``scripts/groww_probe.py`` is the
STANDALONE terminal diagnostic (it imports nothing from ``brutex`` and stays out
of coverage). THIS module is the structured, injectable form of the same LIVE
checks, consumed by the dashboard "Test / verify" button — every check goes
through the neutral :class:`~brutex.adapters.groww.client.GrowwClient` (never the
raw SDK), catches the neutral :class:`~brutex.adapters.groww.errors.GrowwError`,
and returns a structured :class:`ProbeResult` so the route can render each
verdict. It is 100%-covered with a fake ``GrowwClient`` — no ``growwapi``, no
network.

Only the THREE live checks run here (``vix`` / ``granularity`` / ``options``):
the script's ``parity`` / ``drift`` / ``ratelimit`` checks are MANUAL (they need
a second vendor series, a tight loop, or a multi-day re-fetch) and are out of
scope for a single button (Golden Rule #1 — no invented automatic verdict).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final, Protocol

from brutex.adapters.groww.client import GrowwClient, GrowwClientProtocol
from brutex.adapters.groww.constants import EARLIEST_HISTORY
from brutex.adapters.groww.errors import GrowwError


class CandleFetcher(Protocol):
    """The ONE vendor surface the depth probe needs (a structural subset of
    :class:`~brutex.adapters.groww.client.GrowwClientProtocol`, so the real
    client satisfies it and a probe test double stays minimal)."""

    def historical_candles(
        self,
        *,
        exchange: str,
        segment: str,
        groww_symbol: str,
        interval: str,
        start: str,
        end: str,
    ) -> Mapping[str, object]:
        """Fetch one historical-candle window (row-of-arrays payload)."""
        raise NotImplementedError  # structural Protocol stub — never executed


#: India VIX is the HARD-GATE (ADR-0009 risk #3). The exact ``groww_symbol`` is
#: one of the unknowns the probe resolves; these are the documented candidates to
#: try (mirrors ``scripts/groww_probe.py``).
_VIX_SYMBOL_CANDIDATES: Final[tuple[str, ...]] = (
    "NSE-INDIAVIX",
    "NSE-INDIA VIX",
    "NSE-INDIAVIX-IDX",
)

#: Verdict literals — a check is OK (live data returned), ABSENT (the call
#: succeeded but returned no rows / nothing to confirm presence), or ERROR (a
#: neutral :class:`GrowwError` was raised).
VERDICT_OK: Final[str] = "OK"
VERDICT_ABSENT: Final[str] = "ABSENT"
VERDICT_ERROR: Final[str] = "ERROR"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One probe check's structured outcome (renderable as a dashboard row).

    Attributes
    ----------
    name
        The check identifier (``"vix"`` / ``"granularity"`` / ``"options"``).
    verdict
        One of :data:`VERDICT_OK`, :data:`VERDICT_ABSENT`, :data:`VERDICT_ERROR`.
    detail
        A human-readable, secret-free summary line.
    symbol
        The working ``groww_symbol`` (VIX check) when one resolved, else ``None``.
    rows
        Row / item count when the check produced one, else ``None``.
    """

    name: str
    verdict: str
    detail: str
    symbol: str | None = None
    rows: int | None = None


def _candle_rows(resp: object) -> int:
    """Return the number of candles in a historical-candle response payload.

    The Groww historical payload is a ``{"candles": [...]}`` mapping (mirrors
    ``scripts/groww_probe.py``). A non-mapping / missing / non-list ``candles``
    is treated as zero rows (defensive — the probe never trusts payload shape).
    """
    if not isinstance(resp, dict):
        return 0
    candles = resp.get("candles")
    if not isinstance(candles, list):
        return 0
    return len(candles)


def probe_vix(client: GrowwClientProtocol) -> ProbeResult:
    """ADR-0009 §4.1 — HARD-GATE: is India VIX present + fetchable on Groww?

    Tries each candidate ``groww_symbol`` over a short 2024 window; returns OK on
    the FIRST that returns candles (capturing the working symbol). ABSENT if
    every candidate returned empty candles; ERROR on a :class:`GrowwError`.
    """
    for symbol in _VIX_SYMBOL_CANDIDATES:
        try:
            resp = client.historical_candles(
                exchange="NSE",
                segment="CASH",
                groww_symbol=symbol,
                interval="1minute",
                start="2024-01-01 09:15:00",
                end="2024-01-02 15:30:00",
            )
        except GrowwError as exc:
            return ProbeResult(
                name="vix",
                verdict=VERDICT_ERROR,
                detail=f"India VIX probe failed: {exc}",
            )
        rows = _candle_rows(resp)
        if rows:
            return ProbeResult(
                name="vix",
                verdict=VERDICT_OK,
                detail=f"India VIX present as {symbol} ({rows} rows)",
                symbol=symbol,
                rows=rows,
            )
    return ProbeResult(
        name="vix",
        verdict=VERDICT_ABSENT,
        detail=(
            f"India VIX absent: no candidate symbol {list(_VIX_SYMBOL_CANDIDATES)} returned candles"
        ),
    )


def probe_granularity(client: GrowwClient) -> ProbeResult:
    """ADR-0009 §4.2 — do NIFTY 1m rows truly start Jan 2020 (COVID window)?

    Fetches NIFTY 1m over a SINGLE ≤30-day window at the Jan-2020 boundary
    (``2020-01-01..2020-01-30``) and reports the row count + first timestamp.
    The window is deliberately ONE month, NOT a quarter: Groww caps the 1-minute
    interval at **30 days per request** (``CHUNK_WINDOW_30D_INTERVALS`` /
    ``WINDOW_DAYS_30``; ``groww/api-reference/03-backtesting.md``), so the old
    90-day (Q1) request was rejected vendor-side — "Interval 1minute can only be
    queried for a maximum of 30 days" — a FALSE ❌ on a perfectly valid token
    (DEC-GROWW-PROBE-1M-WINDOW-001). One ≤30-day call answers the boundary question
    (does 1m exist at Jan-2020?) without tiling. ABSENT when the window came back
    empty; ERROR on a :class:`GrowwError`.
    """
    try:
        resp = client.historical_candles(
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            interval="1minute",
            start="2020-01-01 09:15:00",
            end="2020-01-30 15:30:00",
        )
    except GrowwError as exc:
        return ProbeResult(
            name="granularity",
            verdict=VERDICT_ERROR,
            detail=f"NIFTY 1m granularity probe failed: {exc}",
        )
    candles = resp.get("candles") if isinstance(resp, dict) else None
    if not isinstance(candles, list) or not candles:
        return ProbeResult(
            name="granularity",
            verdict=VERDICT_ABSENT,
            detail="NIFTY 1m 2020-Jan returned no candles",
            rows=0,
        )
    first = candles[0]
    first_ts = first[0] if isinstance(first, (list, tuple)) and first else first
    return ProbeResult(
        name="granularity",
        verdict=VERDICT_OK,
        detail=f"NIFTY 1m 2020-Jan: {len(candles)} rows, first_ts={first_ts}",
        rows=len(candles),
    )


def probe_options(client: GrowwClient) -> ProbeResult:
    """ADR-0009 §4.6 — expiries → contracts round-trip (lake readiness).

    Lists NIFTY 2020-01 expiries, then the contracts of the first expiry, and
    reports both counts. ABSENT when there are no 2020-01 expiries; ERROR on a
    :class:`GrowwError`.
    """
    try:
        expiries = client.expiries(exchange="NSE", underlying_symbol="NIFTY", year=2020, month=1)
    except GrowwError as exc:
        return ProbeResult(
            name="options",
            verdict=VERDICT_ERROR,
            detail=f"get_expiries failed: {exc}",
        )
    exp_list = expiries.get("expiries") if isinstance(expiries, dict) else None
    if not isinstance(exp_list, list) or not exp_list:
        return ProbeResult(
            name="options",
            verdict=VERDICT_ABSENT,
            detail="no NIFTY 2020-01 expiries returned",
            rows=0,
        )
    try:
        contracts = client.contracts(
            exchange="NSE", underlying_symbol="NIFTY", expiry_date=str(exp_list[0])
        )
    except GrowwError as exc:
        return ProbeResult(
            name="options",
            verdict=VERDICT_ERROR,
            detail=f"get_contracts failed: {exc}",
        )
    con_list = contracts.get("contracts") if isinstance(contracts, dict) else None
    con_count = len(con_list) if isinstance(con_list, list) else 0
    return ProbeResult(
        name="options",
        verdict=VERDICT_OK,
        detail=f"NIFTY 2020-01: {len(exp_list)} expiries, {con_count} contracts",
        rows=con_count,
    )


#: Days per depth-probe window — the 1-minute interval's vendor cap is 30 days per
#: request; a 15-day tile stays comfortably inside it while keeping the bisection
#: predicate simple (a window has rows iff the true floor falls before its end).
_DEPTH_WINDOW_DAYS: Final[int] = 15

#: Hard per-symbol call bound for the depth bisection (kit spec stage 6: ≤ 20).
DEPTH_PROBE_MAX_CALLS: Final[int] = 20


@dataclass(frozen=True, slots=True)
class DepthProbeResult:
    """One symbol's 1-minute history-depth probe outcome (ADR-0009 §3.1 open gate).

    Attributes
    ----------
    symbol
        The probed ``groww_symbol``.
    verdict
        :data:`VERDICT_OK` (a floor was established), :data:`VERDICT_ABSENT`
        (no 1-minute data found anywhere in the probed range), or
        :data:`VERDICT_ERROR` (a :class:`GrowwError` interrupted the probe).
    first_ts
        The raw timestamp of the FIRST 1-minute bar observed at the floor
        window (vendor encoding preserved for the transcript), else ``None``.
    probed_floor
        The first probed calendar day whose window returned rows (the observed
        1-minute floor, to window granularity), else ``None``.
    boundary_has_data
        ``True`` iff the window AT the configured boundary (2020-01-01) itself
        returned rows — the direct GROWW_HISTORY_FLOOR confirmation.
    calls_used
        Vendor calls this probe consumed (bounded by
        :data:`DEPTH_PROBE_MAX_CALLS`).
    detail
        Human-readable, secret-free summary line.
    """

    symbol: str
    verdict: str
    first_ts: str | None
    probed_floor: date | None
    boundary_has_data: bool
    calls_used: int
    detail: str


def _first_row_ts(resp: object) -> str | None:
    """The raw first-candle timestamp of a payload (``None`` when there are no rows)."""
    if not isinstance(resp, dict):
        return None
    candles = resp.get("candles")
    if not isinstance(candles, list) or not candles:
        return None
    first = candles[0]
    if isinstance(first, (list, tuple)) and first:
        return str(first[0])
    return str(first)


def _date_of_first_ts(first_ts: str | None) -> date | None:
    """The calendar date of a raw vendor timestamp string, else ``None``.

    Handles the documented native-IST string shapes (``YYYY-MM-DD…``); an
    epoch-encoded stamp does not parse here and the caller falls back to the
    window start (an honest window-granularity floor, stated in the detail).
    """
    if first_ts is None:
        return None
    try:
        return date.fromisoformat(first_ts[:10])
    except ValueError:
        return None


def probe_depth(
    client: CandleFetcher,
    *,
    symbol: str = "NSE-NIFTY",
    exchange: str = "NSE",
    segment: str = "CASH",
    boundary: date = EARLIEST_HISTORY,
    today: date,
    bisect: bool = False,
    max_calls: int = DEPTH_PROBE_MAX_CALLS,
) -> DepthProbeResult:
    """Probe the TRUE 1-minute history floor for *symbol* (kit stage 6, register R6).

    First fetches ONE :data:`_DEPTH_WINDOW_DAYS`-day 1-minute window at
    *boundary* (2020-01-01). Rows there CONFIRM the configured floor. An empty
    boundary window with ``bisect=False`` reports the shallow-floor hazard
    without further calls; with ``bisect=True`` a BOUNDED binary search over
    day offsets in ``[boundary, today]`` finds the earliest window with data
    (the observed floor) in ``≤ log2(range) + 2`` calls, hard-capped at
    *max_calls* (the property-tested bound).

    The window-has-rows predicate is monotone when vendor history is
    contiguous-from-floor (the honest assumption; a vendor serving old ranges
    INTERMITTENTLY makes any probe a lower bound — stated in the detail line).

    Budget contract (DEC-PROBE-BUDGET-PROLOGUE-001): EVERY vendor call —
    the boundary prologue, the hi-at-today prologue AND the bisection loop —
    honours ``max_calls``. The budget is clamped UP to 1 (``max(max_calls,
    1)`` — "if uncertain, fetch": the single boundary call is always
    permitted, preserving the downstream non-bisect cost-of-exactly-1
    assumption), so the invariant is ``calls_used <= max(max_calls, 1)``.
    A budget exhausted BEFORE the hi-at-today prologue returns the SAME
    honest :data:`VERDICT_ABSENT` (never a fabricated floor) with zero
    further vendor calls.
    """
    calls = 0
    budget = max(max_calls, 1)

    def _window_rows(start_day: date) -> tuple[int, str | None]:
        nonlocal calls
        calls += 1
        window_end = start_day + timedelta(days=_DEPTH_WINDOW_DAYS - 1)
        resp = client.historical_candles(
            exchange=exchange,
            segment=segment,
            groww_symbol=symbol,
            interval="1minute",
            start=f"{start_day.isoformat()} 09:15:00",
            end=f"{window_end.isoformat()} 15:30:00",
        )
        return _candle_rows(resp), _first_row_ts(resp)

    try:
        boundary_rows, boundary_first = _window_rows(boundary)
        if boundary_rows:
            return DepthProbeResult(
                symbol=symbol,
                verdict=VERDICT_OK,
                first_ts=boundary_first,
                probed_floor=_date_of_first_ts(boundary_first) or boundary,
                boundary_has_data=True,
                calls_used=calls,
                detail=(
                    f"{symbol} 1m has data at the {boundary.isoformat()} boundary "
                    f"(first_ts={boundary_first})"
                ),
            )
        if not bisect:
            return DepthProbeResult(
                symbol=symbol,
                verdict=VERDICT_ABSENT,
                first_ts=None,
                probed_floor=None,
                boundary_has_data=False,
                calls_used=calls,
                detail=(
                    f"{symbol} 1m window at {boundary.isoformat()} is EMPTY — true floor "
                    f"unknown (re-run with --depth-bisect to locate it)"
                ),
            )
        # Bounded bisection over day offsets. Invariant: lo's window is EMPTY,
        # hi's window HAS rows. Establish hi AT today first (one call — the
        # window may extend past today, which is harmless); a symbol empty
        # there too has no probed 1m data at all.
        span_days = max((today - boundary).days, 1)
        lo, hi = 0, span_days
        if calls >= budget:
            # Budget exhausted before the hi-at-today prologue: the SAME
            # honest ABSENT verdict — zero further vendor calls, never a
            # fabricated floor (DEC-PROBE-BUDGET-PROLOGUE-001).
            return DepthProbeResult(
                symbol=symbol,
                verdict=VERDICT_ABSENT,
                first_ts=None,
                probed_floor=None,
                boundary_has_data=False,
                calls_used=calls,
                detail=(
                    f"{symbol} 1m boundary window at {boundary.isoformat()} is EMPTY and "
                    f"the call budget ({max_calls}) is exhausted before locating hi — "
                    f"true floor unknown"
                ),
            )
        hi_rows, hi_first = _window_rows(boundary + timedelta(days=hi))
        if not hi_rows:
            return DepthProbeResult(
                symbol=symbol,
                verdict=VERDICT_ABSENT,
                first_ts=None,
                probed_floor=None,
                boundary_has_data=False,
                calls_used=calls,
                detail=(
                    f"{symbol} 1m windows at BOTH {boundary.isoformat()} and near "
                    f"{today.isoformat()} are empty — no probed 1m data at all"
                ),
            )
        floor_first = hi_first
        while hi - lo > 1:
            # ``calls`` is nonlocal-mutated inside ``_window_rows`` — checked at the
            # TOP of each iteration (an explicit break, not a compound loop guard, so
            # a static analyser that cannot see the closure mutation never misreads
            # the budget check as constant).
            if calls >= budget:
                break
            mid = (lo + hi) // 2
            mid_rows, mid_first = _window_rows(boundary + timedelta(days=mid))
            if mid_rows:
                hi, floor_first = mid, mid_first
            else:
                lo = mid
        # The found window STARTS up to (window − 1) days before the true first
        # bar; the first row's own date is the honest floor when parseable
        # (string-stamped), else the window start (window-granularity bound).
        probed_floor = _date_of_first_ts(floor_first) or (boundary + timedelta(days=hi))
        converged = hi - lo <= 1
        return DepthProbeResult(
            symbol=symbol,
            verdict=VERDICT_OK,
            first_ts=floor_first,
            probed_floor=probed_floor,
            boundary_has_data=False,
            calls_used=calls,
            detail=(
                f"{symbol} 1m observed floor ≈ {probed_floor.isoformat()} "
                f"(first_ts={floor_first}; {calls} calls"
                f"{'' if converged else '; call budget hit — floor is an UPPER bound'}; "
                f"a lower bound if the vendor serves old ranges intermittently)"
            ),
        )
    except GrowwError as exc:
        return DepthProbeResult(
            symbol=symbol,
            verdict=VERDICT_ERROR,
            first_ts=None,
            probed_floor=None,
            boundary_has_data=False,
            calls_used=calls,
            detail=f"{symbol} depth probe failed: {exc}",
        )


def run_probe(client: GrowwClient) -> list[ProbeResult]:
    """Run the three LIVE Groww checks (vix + granularity + options) in order.

    The MANUAL checks (parity / drift / ratelimit) are out of scope for the
    dashboard button (they need a second vendor, a tight loop, or a multi-day
    re-fetch); only the automatable live checks run here.
    """
    return [probe_vix(client), probe_granularity(client), probe_options(client)]


__all__ = [
    "DEPTH_PROBE_MAX_CALLS",
    "VERDICT_ABSENT",
    "VERDICT_ERROR",
    "VERDICT_OK",
    "CandleFetcher",
    "DepthProbeResult",
    "ProbeResult",
    "probe_depth",
    "probe_granularity",
    "probe_options",
    "probe_vix",
    "run_probe",
]
