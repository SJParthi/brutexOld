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

from dataclasses import dataclass
from typing import Final

from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.errors import GrowwError

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


def probe_vix(client: GrowwClient) -> ProbeResult:
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

    Fetches NIFTY 1m over 2020-01-01..2020-03-31 and reports the row count +
    first timestamp. ABSENT when the window came back empty; ERROR on a
    :class:`GrowwError`.
    """
    try:
        resp = client.historical_candles(
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            interval="1minute",
            start="2020-01-01 09:15:00",
            end="2020-03-31 15:30:00",
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
            detail="NIFTY 1m 2020-Q1 returned no candles",
            rows=0,
        )
    first = candles[0]
    first_ts = first[0] if isinstance(first, (list, tuple)) and first else first
    return ProbeResult(
        name="granularity",
        verdict=VERDICT_OK,
        detail=f"NIFTY 1m 2020-Q1: {len(candles)} rows, first_ts={first_ts}",
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


def run_probe(client: GrowwClient) -> list[ProbeResult]:
    """Run the three LIVE Groww checks (vix + granularity + options) in order.

    The MANUAL checks (parity / drift / ratelimit) are out of scope for the
    dashboard button (they need a second vendor, a tight loop, or a multi-day
    re-fetch); only the automatable live checks run here.
    """
    return [probe_vix(client), probe_granularity(client), probe_options(client)]


__all__ = [
    "VERDICT_ABSENT",
    "VERDICT_ERROR",
    "VERDICT_OK",
    "ProbeResult",
    "probe_granularity",
    "probe_options",
    "probe_vix",
    "run_probe",
]
