"""Per-underlying PCR rows for the ``/movers`` Options tab (DEC-MOVERS-OPTIONS-PCR-001).

A thin, pure adapter over the COMMON two-side ratio primitive
:mod:`brutex.data.pcr` — READ-ONLY dashboard derivation over the
REFERENCE_ONLY F&O lake (CLAUDE.md §3): never stored, never a
vocabulary condition, never in run identity.

The keystone: :class:`PcrLeg` is captured from the PARSED loop variables
inside :func:`~brutex.dashboard.movers_options.compute_options_candidates`
(NEVER from the display-renamed ``OptionMover``), so a ``names`` rename /
collision can never split or merge a PCR group. Each row carries BOTH the
parsed group key (``underlying``) and the display label the movers table
shows (``display_underlying``), so panel and table always agree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Any

from brutex.data.pcr import PcrResult, compute_pcr

__all__ = ["PcrLeg", "UnderlyingPcr", "compute_pcr_rows", "pcr_row_json"]


@dataclass(frozen=True, slots=True)
class PcrLeg:
    """One successfully-read option leg's PCR slice.

    Attributes
    ----------
    underlying
        The PARSED underlying (the ``parse_fno_symbol`` coordinate) — the
        structural group key, never a display rename.
    opt_type
        ``"CE"`` / ``"PE"`` — anything else trips the fold's
        :class:`ValueError` tripwire (:func:`brutex.data.pcr.fold_side_ratio`).
    oi
        The last as-of bar's ``open_interest``; ``None`` = honestly missing
        (a legacy/null lake cell) — excluded from the OI sums, counted.
    volume
        Cumulative session volume ≤ the as-of cutoff (non-null by the
        session reader's contract).
    """

    underlying: str
    opt_type: str
    oi: int | None
    volume: int


@dataclass(frozen=True, slots=True)
class UnderlyingPcr:
    """One PCR panel row at the tier expiry — parsed key + display label + result.

    Attributes
    ----------
    underlying
        The PARSED group key (structural identity — rename-proof).
    display_underlying
        The ``resolved_names`` transform the movers TABLE shows
        (``== underlying`` when no rename) — panel/table labels always agree.
    expiry
        The ONE tier expiry for this underlying (the tier filter guarantees
        uniqueness per underlying).
    pcr
        The COMMON primitive's result — both variants + coverage, no field
        duplication.
    """

    underlying: str
    display_underlying: str
    expiry: date_cls
    pcr: PcrResult


def compute_pcr_rows(
    legs: Sequence[PcrLeg],
    *,
    legs_total: Mapping[str, int],
    target_expiry: Mapping[str, date_cls],
    display_names: Mapping[str, str] | None = None,
) -> tuple[UnderlyingPcr, ...]:
    """Fold the read legs into per-underlying PCR rows (pure, deterministic).

    Emits ONE row per *legs_total* key (the authoritative tier universe) —
    INCLUDING an underlying whose EVERY leg was skipped (``legs_read=0``,
    zero sums, both statuses ``zero_denominator``, coverage ``"empty"``):
    a chain that exists but was unreadable is REPORTED, never silently
    absent (the #919 silent-vanish lesson, read-side; Golden Rule #1).
    Rows are sorted by the PARSED ``underlying`` (deterministic payload).

    Parameters
    ----------
    legs
        The successfully-read legs (captured from the parsed loop variables).
    legs_total
        Parsed underlying → tier chain size, counted BEFORE any read.
    target_expiry
        Parsed underlying → the ONE tier expiry.
    display_names
        Parsed → display transform (the movers table's ``resolved_names``);
        ``None`` ⇒ identity.

    Returns
    -------
    tuple[UnderlyingPcr, ...]
        One row per *legs_total* key, sorted by parsed underlying.

    Raises
    ------
    ValueError
        When a leg's ``underlying`` is not a *legs_total* key, or a
        per-underlying ``legs_total`` undercounts its read group (via
        :class:`~brutex.data.pcr.PcrCoverage`) — a denominator wiring bug
        must trip, never read as fake-full coverage.
    KeyError
        When a *legs_total* key is missing from *target_expiry* (propagated
        — the tier filter guarantees the invariant upstream).
    """
    groups: dict[str, list[PcrLeg]] = {}
    for leg in legs:
        if leg.underlying not in legs_total:
            raise ValueError(
                f"PCR leg underlying {leg.underlying!r} is not a legs_total key — "
                "a denominator wiring mismatch"
            )
        groups.setdefault(leg.underlying, []).append(leg)
    names = display_names or {}
    rows: list[UnderlyingPcr] = []
    for underlying in sorted(legs_total):
        group: Sequence[PcrLeg] = groups.get(underlying, ())
        rows.append(
            UnderlyingPcr(
                underlying=underlying,
                display_underlying=names.get(underlying, underlying),
                expiry=target_expiry[underlying],
                pcr=compute_pcr(
                    group,
                    side_of=lambda leg: leg.opt_type,
                    oi_of=lambda leg: leg.oi,
                    volume_of=lambda leg: leg.volume,
                    legs_total=legs_total[underlying],
                ),
            )
        )
    return tuple(rows)


def pcr_row_json(row: UnderlyingPcr) -> dict[str, Any]:
    """Serialize one :class:`UnderlyingPcr` row to JSON-native types.

    Ratios ride as FULL-PRECISION strings (the ``pct_change`` precedent —
    display trimming is frontend-only; DEC-MONEY-2DP-001 forbids rounding a
    statistical metric server-side) or ``None`` → JSON ``null``; the typed
    :class:`~brutex.data.pcr.RatioStatus` sentinels as their enum strings;
    sums/counters as raw ints; the expiry as ISO ``YYYY-MM-DD``; plus the
    coverage label and the COMMON server-composed
    :meth:`~brutex.data.pcr.PcrResult.coverage_note`.

    Parameters
    ----------
    row
        The per-underlying PCR row to serialize.

    Returns
    -------
    dict[str, Any]
        The wire-shape dict (see the DEC-MOVERS-OPTIONS-PCR-001 contract).
    """
    pcr = row.pcr
    return {
        "underlying": row.underlying,
        "display_underlying": row.display_underlying,
        "expiry": row.expiry.isoformat(),
        "oi_pcr": str(pcr.oi_pcr) if pcr.oi_pcr is not None else None,
        "oi_status": pcr.oi.status.value,
        "vol_pcr": str(pcr.vol_pcr) if pcr.vol_pcr is not None else None,
        "vol_status": pcr.volume.status.value,
        "put_oi": pcr.put_oi,
        "call_oi": pcr.call_oi,
        "put_volume": pcr.put_volume,
        "call_volume": pcr.call_volume,
        "oi_missing_legs": pcr.oi.missing_legs,
        "volume_missing_legs": pcr.volume.missing_legs,
        "legs_read": pcr.coverage.legs_read,
        "legs_total": pcr.coverage.legs_total,
        "coverage": pcr.coverage_label,
        "coverage_note": pcr.coverage_note(),
    }
