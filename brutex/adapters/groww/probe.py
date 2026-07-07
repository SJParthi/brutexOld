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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final, Protocol

from brutex.adapters.groww.client import GrowwClient, GrowwClientProtocol
from brutex.adapters.groww.constants import EARLIEST_HISTORY
from brutex.adapters.groww.contracts import underlying_symbol_forms
from brutex.adapters.groww.errors import GrowwError
from brutex.models import Instrument, Segment


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


# ────────────────────────────────────────────────────────────────────────
# Ampersand (%26) symbol-form probe — DEC-FORM-WINNER-PERSIST-001
# ────────────────────────────────────────────────────────────────────────

#: Per-underlying probe verdicts (the ``&``-form evidence vocabulary). PLAIN_OK =
#: only the plain ``&`` form returned data AND every encoded call was a CLEAN
#: empty (the no-data claim about the encoded side is uncontaminated); ENCODED_ONLY
#: = only the ``%26`` form did, with every plain call a clean empty (the #898
#: hypothesis demonstrated for that name); BOTH_OK = both forms returned data (a
#: positive claim about each side — an incidental sibling error cannot undermine
#: it); PLAIN_OK_UNCONFIRMED / ENCODED_ONLY_UNCONFIRMED = one side returned data
#: but the OTHER side RAISED (throttle/5xx), so "the other form CANNOT return
#: data" was NOT cleanly observed — the winner still seeds (order optimisation is
#: harmless) but the verdict is NON-clear and can never stamp (Golden Rule #1: a
#: throttled side is not a refuted side); NEITHER = every call was a clean empty;
#: ERROR = no data and at least one call raised. MIXED is an AGGREGATE-only
#: verdict — a family whose per-underlying verdicts disagree (never assigned to a
#: single underlying).
AMP_VERDICT_PLAIN_OK: Final[str] = "PLAIN_OK"
AMP_VERDICT_ENCODED_ONLY: Final[str] = "ENCODED_ONLY"
AMP_VERDICT_BOTH_OK: Final[str] = "BOTH_OK"
AMP_VERDICT_PLAIN_OK_UNCONFIRMED: Final[str] = "PLAIN_OK_UNCONFIRMED"
AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED: Final[str] = "ENCODED_ONLY_UNCONFIRMED"
AMP_VERDICT_NEITHER: Final[str] = "NEITHER"
AMP_VERDICT_ERROR: Final[str] = "ERROR"
AMP_VERDICT_MIXED: Final[str] = "MIXED"

#: The clear (decisive-capable) per-underlying verdicts — BOTH_OK counts
#: decisive-for-plain (the vendor handles the raw ``&`` for that name). The
#: *_UNCONFIRMED pair is deliberately EXCLUDED: a verdict whose no-data side was
#: error-contaminated may seed a winner but may never contribute to a DECISIVE
#: report (and therefore never to the machine-readable stamp) — re-run the probe
#: when the vendor is healthy instead.
AMP_CLEAR_VERDICTS: Final[frozenset[str]] = frozenset(
    {AMP_VERDICT_PLAIN_OK, AMP_VERDICT_ENCODED_ONLY, AMP_VERDICT_BOTH_OK}
)

#: Verdicts whose evidence names a WINNING form (used for durable-map seeding):
#: verdict → the winner's index in the canonical forms tuple (plain=0, encoded=1).
#: The *_UNCONFIRMED pair seeds too — data on one side is real evidence for the
#: ORDER optimisation even when the sibling side errored (a wrong seed costs one
#: extra empty fetch, never data) — it just never counts CLEAR above.
_AMP_WINNER_INDEX: Final[Mapping[str, int]] = {
    AMP_VERDICT_PLAIN_OK: 0,
    AMP_VERDICT_BOTH_OK: 0,
    AMP_VERDICT_PLAIN_OK_UNCONFIRMED: 0,
    AMP_VERDICT_ENCODED_ONLY: 1,
    AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED: 1,
}

#: Form roles, positionally aligned with the CANONICAL
#: :func:`~brutex.adapters.groww.contracts.underlying_symbol_forms` tuple.
_AMP_ROLES: Final[tuple[str, str]] = ("plain", "encoded")


class ProbeCallBudget(Protocol):
    """The bounded vendor-call budget seam (a structural subset of
    :class:`brutex.doctor_live.CallBudget`, so the kit's real budget satisfies it
    without this leaf importing the kit — no cycle)."""

    def spend(self, count: int = 1, *, label: str) -> None:
        """Spend *count* calls, raising when the hard cap would be exceeded."""
        raise NotImplementedError  # structural Protocol stub — never executed


class ProbeTranscript(Protocol):
    """The evidence-line seam (a structural subset of
    :class:`brutex.doctor_live.Transcript` — canonical sorted-key JSON per event)."""

    def event(self, name: str, /, **fields: object) -> None:
        """Append one transcript event."""
        raise NotImplementedError  # structural Protocol stub — never executed


@dataclass(frozen=True, slots=True)
class AmpersandFormCall:
    """One physical probe call's evidence (also mirrored to the transcript).

    Attributes
    ----------
    underlying
        The ``&``-underlying being probed (e.g. ``"M&M"``).
    form
        The exact symbol form the call was ISSUED with (plain or ``%26``-encoded).
    role
        ``"plain"`` or ``"encoded"`` — the form's role in the canonical tuple.
    api
        ``"expiries"`` or ``"contracts"`` — which vendor surface was called.
    count
        Returned item count (``None`` when the call raised).
    error
        The neutral :class:`~brutex.adapters.groww.errors.GrowwError` shape
        (``"Type: message"``), or ``None`` on a clean return.
    """

    underlying: str
    form: str
    role: str
    api: str
    count: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class AmpersandProbeReport:
    """The whole ``&``-family probe outcome (DEC-FORM-WINNER-PERSIST-001).

    Attributes
    ----------
    results
        Every physical call's :class:`AmpersandFormCall`, in issue order.
    per_underlying
        ``underlying → verdict`` (:data:`AMP_VERDICT_PLAIN_OK` /
        :data:`AMP_VERDICT_ENCODED_ONLY` / :data:`AMP_VERDICT_BOTH_OK` /
        :data:`AMP_VERDICT_PLAIN_OK_UNCONFIRMED` /
        :data:`AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED` /
        :data:`AMP_VERDICT_NEITHER` / :data:`AMP_VERDICT_ERROR`).
    winners
        ``underlying → the winning FORM string`` for every underlying whose
        evidence names a winner — the CLEAR verdicts AND the *_UNCONFIRMED pair
        (data on one side seeds the ORDER optimisation even when the sibling
        side errored — a wrong seed costs one extra empty fetch, never data);
        a NEITHER/ERROR underlying has no entry (nothing is fabricated).
    overall
        The family aggregate: the single shared verdict when every underlying
        agrees, :data:`AMP_VERDICT_MIXED` when clear verdicts disagree or clear
        and unclear mix, :data:`AMP_VERDICT_NEITHER` for an empty family.
    decisive
        ``True`` iff the family is non-empty and EVERY underlying's verdict is
        CLEAR (:data:`AMP_CLEAR_VERDICTS` — BOTH_OK counts decisive-for-plain;
        an *_UNCONFIRMED verdict — data on one side, a RAISED error on the other
        — is deliberately NON-clear: the losing side was never cleanly observed,
        so nothing may be stamped from it). Only a decisive report may write the
        machine-readable verdict stamp
        (:data:`~brutex.adapters.groww.symbol_form_preference.FORM_PROBE_VERDICT_KEY`).
    """

    results: tuple[AmpersandFormCall, ...]
    per_underlying: Mapping[str, str]
    winners: Mapping[str, str]
    overall: str
    decisive: bool


def ampersand_family(instruments: Iterable[Instrument]) -> tuple[str, ...]:
    """The ``&``-underlyings to probe, DERIVED from the instrument master — never hardcoded.

    Scans the FNO legs' ``underlying_symbol`` (the vendor's own contract set — the
    proof the name is F&O-eligible) for ``&``, deduped + sorted for determinism.
    DYNAMIC (CLAUDE.md §11): a future ``X&Y`` listing is auto-probed with zero code
    change, exactly as :func:`~brutex.adapters.groww.contracts.underlying_symbol_forms`
    auto-covers it in production.
    """
    family = {
        instrument.underlying_symbol
        for instrument in instruments
        if instrument.segment is Segment.FNO
        and instrument.underlying_symbol is not None
        and "&" in instrument.underlying_symbol
    }
    return tuple(sorted(family))


def _array_len(payload: object, key: str) -> int:
    """Defensive item count of a ``{"expiries"|"contracts": [...]}`` payload."""
    if not isinstance(payload, Mapping):
        return 0
    items = payload.get(key)
    if not isinstance(items, (list, tuple)):
        return 0
    return len(items)


def _nearest_expiry(payloads: Iterable[object]) -> str | None:
    """The earliest parseable ISO expiry across *payloads* (``None`` when there is none).

    Defensive: unparseable / non-string entries are SKIPPED (the probe records
    counts, it never fail-louds on shape — a malformed expiry simply cannot seed
    the ``get_contracts`` leg).
    """
    nearest: date | None = None
    nearest_raw: str | None = None
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        items = payload.get("expiries")
        if not isinstance(items, (list, tuple)):
            continue
        for item in items:
            if not isinstance(item, str):
                continue
            try:
                parsed = date.fromisoformat(item)
            except ValueError:
                continue
            if nearest is None or parsed < nearest:
                nearest, nearest_raw = parsed, item
    return nearest_raw


def _emit_call(
    transcript: ProbeTranscript,
    results: list[AmpersandFormCall],
    calls: list[AmpersandFormCall],
    call: AmpersandFormCall,
) -> None:
    """Mirror one physical call to the transcript + both evidence lists.

    The transcript event fires FIRST — BEFORE any verdict interpretation — so the
    evidence line exists even if a later step aborts (the kit's "EVIDENCE OR IT
    DID NOT HAPPEN" ordering pin).
    """
    transcript.event(
        "amp.call",
        underlying=call.underlying,
        form=call.form,
        role=call.role,
        api=call.api,
        count=call.count,
        error=call.error,
    )
    results.append(call)
    calls.append(call)


def _amp_verdict(calls: Sequence[AmpersandFormCall]) -> str:
    """Fold one underlying's calls into its verdict.

    Data on a side is POSITIVE evidence for that side and always wins over a
    sibling error (the winner can seed). But the one-sided CLEAR verdicts
    (PLAIN_OK / ENCODED_ONLY) additionally assert the NEGATIVE — "the other form
    returns NO data" — which is demonstrated only when every call on that other
    side was a CLEAN empty: a raised throttle/5xx on the no-data side leaves the
    negative claim UNOBSERVED, so the verdict demotes to the matching
    *_UNCONFIRMED (non-clear → never decisive → never stamped; Golden Rule #1 —
    a 429 is not a refutation). BOTH_OK needs no such guard: it makes two
    positive claims, each proven by its own returned data.
    """
    plain_data = any(c.role == "plain" and (c.count or 0) > 0 for c in calls)
    encoded_data = any(c.role == "encoded" and (c.count or 0) > 0 for c in calls)
    plain_errored = any(c.role == "plain" and c.error is not None for c in calls)
    encoded_errored = any(c.role == "encoded" and c.error is not None for c in calls)
    if plain_data and encoded_data:
        return AMP_VERDICT_BOTH_OK
    if plain_data:
        return AMP_VERDICT_PLAIN_OK_UNCONFIRMED if encoded_errored else AMP_VERDICT_PLAIN_OK
    if encoded_data:
        return AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED if plain_errored else AMP_VERDICT_ENCODED_ONLY
    if plain_errored or encoded_errored:
        return AMP_VERDICT_ERROR
    return AMP_VERDICT_NEITHER


def probe_ampersand_forms(
    client: GrowwClientProtocol,
    underlyings: Sequence[str],
    *,
    exchange: str = "NSE",
    budget: ProbeCallBudget,
    transcript: ProbeTranscript,
) -> AmpersandProbeReport:
    """Probe BOTH symbol forms of every ``&``-underlying against BOTH F&O APIs.

    The live-evidence closure for the #898 UNVERIFIED ``%26`` hypothesis
    (DEC-FORM-WINNER-PERSIST-001). Unlike production (first-non-empty
    short-circuit, ``contracts._fetch_first_nonempty_form``), the probe calls BOTH
    forms UNCONDITIONALLY — evidence for both directions — on ``get_expiries``,
    then BOTH forms of ``get_contracts`` on the NEAREST expiry either form
    returned (no expiry from either form ⇒ the contracts leg is skipped for that
    name — never a fabricated expiry). ≤ 4 vendor calls per underlying, every one
    spent against *budget* BEFORE it is issued (a
    :class:`brutex.doctor_live.CallBudgetExceeded` aborts the run bounded, never
    the account) and mirrored to *transcript* as ONE ``amp.call`` event with the
    RAW count/error BEFORE any verdict interpretation ("EVIDENCE OR IT DID NOT
    HAPPEN"). A raised :class:`~brutex.adapters.groww.errors.GrowwError` is
    recorded as that call's evidence; a non-Groww exception propagates (a code
    bug stays loud). A defensive single-form name (no ``&``) is SKIPPED — the
    probe never invents a second form.
    """
    results: list[AmpersandFormCall] = []
    per_underlying: dict[str, str] = {}
    winners: dict[str, str] = {}
    for underlying in underlyings:
        forms = underlying_symbol_forms(underlying)
        if len(forms) < len(_AMP_ROLES):
            transcript.event("amp.skipped_single_form", underlying=underlying)
            continue
        calls: list[AmpersandFormCall] = []
        expiry_payloads: list[object] = []
        for role, form in zip(_AMP_ROLES, forms, strict=True):
            budget.spend(label=f"amp.expiries.{underlying}")
            try:
                payload: object = client.expiries(exchange=exchange, underlying_symbol=form)
            except GrowwError as exc:
                call = AmpersandFormCall(
                    underlying=underlying,
                    form=form,
                    role=role,
                    api="expiries",
                    count=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                expiry_payloads.append(payload)
                call = AmpersandFormCall(
                    underlying=underlying,
                    form=form,
                    role=role,
                    api="expiries",
                    count=_array_len(payload, "expiries"),
                    error=None,
                )
            _emit_call(transcript, results, calls, call)
        nearest = _nearest_expiry(expiry_payloads)
        if nearest is not None:
            for role, form in zip(_AMP_ROLES, forms, strict=True):
                budget.spend(label=f"amp.contracts.{underlying}")
                try:
                    payload = client.contracts(
                        exchange=exchange, underlying_symbol=form, expiry_date=nearest
                    )
                except GrowwError as exc:
                    call = AmpersandFormCall(
                        underlying=underlying,
                        form=form,
                        role=role,
                        api="contracts",
                        count=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                else:
                    call = AmpersandFormCall(
                        underlying=underlying,
                        form=form,
                        role=role,
                        api="contracts",
                        count=_array_len(payload, "contracts"),
                        error=None,
                    )
                _emit_call(transcript, results, calls, call)
        verdict = _amp_verdict(calls)
        per_underlying[underlying] = verdict
        winner_index = _AMP_WINNER_INDEX.get(verdict)
        if winner_index is not None:
            winners[underlying] = forms[winner_index]
        transcript.event("amp.verdict", underlying=underlying, verdict=verdict)
    distinct = set(per_underlying.values())
    if not distinct:
        overall = AMP_VERDICT_NEITHER
    elif len(distinct) == 1:
        overall = next(iter(distinct))
    else:
        overall = AMP_VERDICT_MIXED
    decisive = bool(per_underlying) and distinct <= AMP_CLEAR_VERDICTS
    return AmpersandProbeReport(
        results=tuple(results),
        per_underlying=per_underlying,
        winners=winners,
        overall=overall,
        decisive=decisive,
    )


def run_probe(client: GrowwClient) -> list[ProbeResult]:
    """Run the three LIVE Groww checks (vix + granularity + options) in order.

    The MANUAL checks (parity / drift / ratelimit) are out of scope for the
    dashboard button (they need a second vendor, a tight loop, or a multi-day
    re-fetch); only the automatable live checks run here.
    """
    return [probe_vix(client), probe_granularity(client), probe_options(client)]


__all__ = [
    "AMP_VERDICT_BOTH_OK",
    "AMP_VERDICT_ENCODED_ONLY",
    "AMP_VERDICT_ENCODED_ONLY_UNCONFIRMED",
    "AMP_VERDICT_ERROR",
    "AMP_VERDICT_MIXED",
    "AMP_VERDICT_NEITHER",
    "AMP_VERDICT_PLAIN_OK",
    "AMP_VERDICT_PLAIN_OK_UNCONFIRMED",
    "DEPTH_PROBE_MAX_CALLS",
    "VERDICT_ABSENT",
    "VERDICT_ERROR",
    "VERDICT_OK",
    "AmpersandFormCall",
    "AmpersandProbeReport",
    "CandleFetcher",
    "DepthProbeResult",
    "ProbeCallBudget",
    "ProbeResult",
    "ProbeTranscript",
    "ampersand_family",
    "probe_ampersand_forms",
    "probe_depth",
    "probe_granularity",
    "probe_options",
    "probe_vix",
    "run_probe",
]
