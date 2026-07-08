"""Date-keyed SPAN margin regime table + margin-true ROI verdicts (R47).

DEC-SWEEP-RESULTS-DEPTH-001 (WP3). Mirrors the DEC-ETC-REGIME-HISTORY-001
idiom field-for-field (``brutex/costs/constants.py``: ``RegimeRow`` /
``validate_regime_table`` / ``rate_for_date``): a strictly-ascending dated
table whose rows carry EITHER a verified per-lot SPAN margin (paisa) with its
citation OR an explicit rate-less REFUSAL window — the
charge-an-unverified-margin state is UNREPRESENTABLE.

## Honest v1: ZERO verified rows (Golden Rule #1)

Recon found NO sourceable SPAN history anywhere — nothing in code, no NSE
SPAN-archive integration, and ``docs/track2/TRACK2_RISK_MGMT.md`` §2.3
explicitly FORBIDS hard-coded margin constants ("must be computed live from
broker SPAN file at backtest time"). So every shipped table is ONE refusal
row from 2020-01-01: :func:`margin_for_date` raises the typed
:class:`UnverifiedMarginRegimeError` for every date in the data window, and
the ROI surface (:func:`roi_on_margin` / :func:`margin_roi_payload`) renders
``status="unverified"`` — R47's mechanism delivered WITHOUT an invented
number. NO runtime escape hatch; the ONLY sanctioned override paths are DATA
(the locked ETC rule): (a) an exchange-archive/circular-cited
:class:`MarginRegimeRow`, or (b) an ADR-stamped row with
``source="ASSUMED per ADR-00XX"``.

O(log N) bisect per lookup (N == 1 today — effectively O(1) per winner,
never a literal-O(1)-total claim). Statistical honesty: ``roi_pct`` keeps
FULL float precision (never rounded — CLAUDE.md §4).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from operator import attrgetter
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Mapping

__all__ = [
    "MARGIN_HISTORY_START",
    "MARGIN_REGIMES",
    "MarginRegimeRow",
    "MarginRegimeTable",
    "MarginRoiVerdict",
    "UnverifiedMarginRegimeError",
    "margin_for_date",
    "margin_roi_payload",
    "roi_on_margin",
    "validate_margin_table",
]


class UnverifiedMarginRegimeError(ValueError):
    """Raised when the margin regime row selected for a date is a refusal row.

    Carries the full context (instrument, date, refusal window, source) so a
    caller can render the honest ``unverified`` verdict without re-deriving
    anything — the :class:`brutex.costs.types.UnverifiedCostRegimeError`
    template, margin-flavoured.
    """

    def __init__(
        self,
        *,
        instrument: str,
        on_date: date,
        window_start: date,
        verified_from: date | None,
        source: str,
    ) -> None:
        self.instrument = instrument
        self.on_date = on_date
        self.window_start = window_start
        self.verified_from = verified_from
        self.source = source
        until = f"until {verified_from}" if verified_from is not None else "with no verified end"
        super().__init__(
            f"SPAN margin for {instrument} on {on_date} is UNVERIFIED "
            f"(refusal window from {window_start} {until}): {source}"
        )


@dataclass(frozen=True, slots=True)
class MarginRegimeRow:
    """One dated SPAN-margin regime row.

    ``span_per_lot_paisa=None`` == an explicit UNVERIFIED refusal window;
    a cited-but-unverified figure lives in ``source`` as text, never in
    ``span_per_lot_paisa``.

    Raises
    ------
    ValueError
        On a non-positive margin (a zero margin would fabricate an infinite
        ROI) or a blank/whitespace-only source.
    """

    start: date
    span_per_lot_paisa: int | None
    source: str

    def __post_init__(self) -> None:
        if self.span_per_lot_paisa is not None and self.span_per_lot_paisa < 1:
            raise ValueError(
                f"MarginRegimeRow(start={self.start}): span_per_lot_paisa "
                f"{self.span_per_lot_paisa} must be >= 1 or None (refusal row) — "
                "a zero margin would fabricate an infinite ROI"
            )
        if not self.source.strip():
            raise ValueError(
                f"MarginRegimeRow(start={self.start}): source must carry its citation "
                f"or its honest citation gap — got {self.source!r}"
            )

    @property
    def verified(self) -> bool:
        """Whether this row carries a verified margin (DERIVED — can never drift)."""
        return self.span_per_lot_paisa is not None


#: A margin regime table: rows sorted strictly ascending by ``start``; the row
#: in force on date D is the LATEST row with ``start <= D``.
MarginRegimeTable = tuple[MarginRegimeRow, ...]


def validate_margin_table(rows: MarginRegimeTable, *, label: str) -> MarginRegimeTable:
    """Fail-loud structural validation, applied ONCE at import per shipped table.

    Raises
    ------
    ValueError
        If the table is empty, or its ``start`` dates are not STRICTLY
        ascending (one check covers unsorted AND duplicate-start rows).
    """
    if not rows:
        raise ValueError(f"margin regime table {label!r} is empty")
    for prev, cur in pairwise(rows):
        if cur.start <= prev.start:
            raise ValueError(
                f"margin regime table {label!r}: row starts must be strictly "
                f"ascending — got {prev.start} then {cur.start}"
            )
    return rows


#: The data-window anchor: the pull floor (GROWW_HISTORY_FLOOR, CLAUDE.md §2
#: Rule 3) — every engine bar is on/after this date, so one refusal row here
#: covers the whole sweepable history.
MARGIN_HISTORY_START: Final[date] = date(2020, 1, 1)

_UNSOURCED: Final[str] = (
    "UNVERIFIED — no sourced NSE/BSE SPAN margin archive is integrated; "
    "TRACK2_RISK_MGMT.md §2.3 forbids hardcoded margin constants; closable only "
    "by per-day exchange SPAN files or an ADR-stamped ASSUMED row"
)

#: The v1 all-refusal regime tables — the three ENGINE instruments
#: (NIFTY/BANKNIFTY/SENSEX, CLAUDE.md §2 Rule 3). Adding a verified row is a
#: DATA change riding a citation, never a code branch.
MARGIN_REGIMES: Final[Mapping[str, MarginRegimeTable]] = {
    "NIFTY": validate_margin_table(
        (MarginRegimeRow(MARGIN_HISTORY_START, None, _UNSOURCED),), label="NIFTY SPAN"
    ),
    "BANKNIFTY": validate_margin_table(
        (MarginRegimeRow(MARGIN_HISTORY_START, None, _UNSOURCED),), label="BANKNIFTY SPAN"
    ),
    "SENSEX": validate_margin_table(
        (MarginRegimeRow(MARGIN_HISTORY_START, None, _UNSOURCED),), label="SENSEX SPAN"
    ),
}


def margin_for_date(
    instrument: str,
    on_date: date,
    *,
    regimes: Mapping[str, MarginRegimeTable] | None = None,
) -> int:
    """Return the verified per-lot SPAN margin (paisa) in force on *on_date*.

    Parameters
    ----------
    instrument
        The regime-table key (``"NIFTY"`` / ``"BANKNIFTY"`` / ``"SENSEX"``).
    on_date
        The trade's regime-selection date (entry date, the DEC-COST-002 law).
    regimes
        Injectable table mapping (tests / future sourced data); ``None``
        reads the shipped :data:`MARGIN_REGIMES`.

    Raises
    ------
    ValueError
        For an instrument with no regime table, or a date preceding the
        recorded history — no margin is ever fabricated (Golden Rule #1).
    UnverifiedMarginRegimeError
        If the selected row is a refusal row — REACHABLE and load-bearing
        for every date today (the v1 tables are all-refusal).
    """
    tables = MARGIN_REGIMES if regimes is None else regimes
    table = tables.get(instrument)
    if table is None:
        raise ValueError(
            f"margin_for_date: no SPAN margin regime table for instrument "
            f"{instrument!r} (known: {sorted(tables)}) — refusing to fabricate a margin"
        )
    if on_date < table[0].start:
        raise ValueError(
            f"margin_for_date: on_date {on_date} precedes the first recorded "
            f"{instrument} margin regime ({table[0].start}) — refusing to fabricate a margin"
        )
    idx = bisect_right(table, on_date, key=attrgetter("start")) - 1
    row = table[idx]
    if row.span_per_lot_paisa is None:
        raise UnverifiedMarginRegimeError(
            instrument=instrument,
            on_date=on_date,
            window_start=row.start,
            verified_from=(table[idx + 1].start if idx + 1 < len(table) else None),
            source=row.source,
        )
    return row.span_per_lot_paisa


@dataclass(frozen=True, slots=True)
class MarginRoiVerdict:
    """The honest margin-true-ROI answer for one winner.

    ``status="ok"`` carries the FULL-precision ``roi_pct`` (net P&L over the
    SPAN margin employed); ``status="unverified"`` carries ``roi_pct=None``
    plus the ``detail`` naming exactly WHY no number exists (R47 verbatim —
    "honest UNVERIFIED label where historical margin cannot be sourced").
    """

    status: Literal["ok", "unverified"]
    roi_pct: float | None
    detail: str


def roi_on_margin(
    net_pnl_paisa: int,
    *,
    instrument: str,
    entry_date: date,
    lots: int = 1,
    regimes: Mapping[str, MarginRegimeTable] | None = None,
) -> MarginRoiVerdict:
    """ROI on the actual SPAN margin employed — or the honest refusal verdict.

    NEVER raises for a missing/unverified regime: an unknown instrument, a
    pre-history date and a refusal window ALL degrade to the ``unverified``
    verdict (a display surface must render, not crash — the LOT_SIZE_UNKNOWN
    skip-and-count precedent). A caller bug (``lots < 1``) still raises.

    Raises
    ------
    ValueError
        If *lots* is below 1 (programmer error, not a data gap).
    """
    if lots < 1:
        raise ValueError(f"roi_on_margin: lots must be >= 1, got {lots}")
    try:
        per_lot = margin_for_date(instrument, entry_date, regimes=regimes)
    except UnverifiedMarginRegimeError as exc:
        return MarginRoiVerdict(status="unverified", roi_pct=None, detail=str(exc))
    except ValueError as exc:
        return MarginRoiVerdict(status="unverified", roi_pct=None, detail=str(exc))
    roi_pct = net_pnl_paisa / (per_lot * lots) * 100.0
    return MarginRoiVerdict(
        status="ok",
        roi_pct=roi_pct,
        detail=(
            f"net P&L {net_pnl_paisa} paisa over SPAN {per_lot} paisa/lot × {lots} lot(s) "
            f"({instrument}, regime as of {entry_date})"
        ),
    )


def margin_roi_payload(
    net_pnl_paisa: int,
    *,
    instrument: str,
    entry_date: date,
    lots: int = 1,
    regimes: Mapping[str, MarginRegimeTable] | None = None,
) -> dict[str, object]:
    """The :func:`roi_on_margin` verdict as the top-100 ``margin_roi`` wire shape.

    The exact ``{"status": …, "roi_pct": …, "detail": …}`` mapping the
    ``top100_artifact.MarginRoiProvider`` seam expects (design B5) — the WP6
    CLI adapter closes over this function as the production provider.
    """
    verdict = roi_on_margin(
        net_pnl_paisa,
        instrument=instrument,
        entry_date=entry_date,
        lots=lots,
        regimes=regimes,
    )
    return {"status": verdict.status, "roi_pct": verdict.roi_pct, "detail": verdict.detail}
