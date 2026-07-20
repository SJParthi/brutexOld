"""Versioned regime-taxonomy leaf — DEC-REGIME-TAXONOMY-001.

A PURE, stdlib-only classification vocabulary for stratifying trading
sessions along three axes — VOLATILITY (VIX band), TREND (session-lens
token), and STRUCTURAL ERA (dated market-structure regimes) — plus two
CRISIS overlays that annotate, never partition. Callers pass values in;
this module performs NO lake I/O and imports NO engine/polars surface.

Versioning law
--------------
:data:`TAXONOMY_VERSION` is the taxonomy's schema version. ANY change to a
band boundary, an era boundary date/label, the crisis-overlay windows, or
the enum vocabularies MUST bump it — a consuming gate folds this version
into its config so a taxonomy change re-keys downstream artifacts instead
of silently re-labeling them.

Design deviation (recorded, justified)
--------------------------------------
The earlier plan hoisted the daily trend CLASSIFIER into this leaf. That
was deliberately NOT done: the classifier depends on the ``trading_core``
Rust wheel and already has exactly one home —
``brutex/engine/session_lens.py`` (``build_session_lens`` over
``trading_core.MarketStructureState`` with the production fractal
half-window ``k = 2``; its own ci gate file). Duplicating it here would
violate CLAUDE.md §11 COMMON and drag the wheel into a stdlib-only leaf.
This leaf instead CONSUMES the classifier's OUTPUT vocabulary
(:func:`trend_stratum_from_lens_token` maps the ``TrendRegime`` value
tokens ``"up"``/``"down"``/``"sideways"`` + the combo-aggregates
``"unknown"`` bucket) — §11 satisfied by consumption, not duplication.
ZERO source/engine files outside this module + its test file are touched
(docs/MIGRATION.md is the customary union-file exception).

Structural-era shape precedent
------------------------------
:class:`EraBoundary` follows the ``brutex/costs/constants.py``
``RegimeRow`` refusal precedent (DEC-ETC-REGIME-HISTORY-001): every row
carries its citation OR its honest citation gap in ``source``, an
unverified row is EXPLICITLY stamped (``verified=False`` and the literal
token ``"UNVERIFIED"`` in ``source`` — enforced by
:func:`validate_era_boundaries` at import), and a fact is never
fabricated to make a row look verified (Golden Rule #1).

Honest limits (Golden Rule #10)
-------------------------------
* ZERO consumers in this PR — the leaf ships INERT by construction; run
  identity, the 60-bit vocabulary, ranking and every engine surface are
  untouched.
* The ``post_peak_margin_full`` era boundary (2021-09-01) ships
  ``verified=False`` — reconstruction is NOT verification (the
  DEC-ETC-REGIME-HISTORY-001 precedent): the circular id + phase dates
  (SEBI/HO/MRD2/DCAP/CIR/P/2020/127, phase-in 25% Dec-2020 → 100%
  2021-09-01) are SECONDARY-corroborated only; the primary SEBI
  circular PDF was not retrieved, so the row is honestly stamped
  ``"UNVERIFIED — …"`` until it is.
* The VIX band edges (13 / 17 / 25) and the era/crisis window dates are
  LABELING CONVENTIONS pending operator calibration — deliberately NOT
  winner-relative and NOT official regulatory windows (except where a
  circular is cited); no future-performance claim is made or implied.
* The COVID-2020 overlay window is a chosen labeling convention, not an
  official crisis declaration.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import Enum
from itertools import pairwise
from typing import Final

__all__ = [
    "COVID_2020_END",
    "COVID_2020_START",
    "ERA_BOUNDARIES",
    "EXTREME_VOL_OVERLAY_START",
    "TAXONOMY_VERSION",
    "VIX_HIGH_MAX",
    "VIX_LOW_MAX",
    "VIX_MID_MAX",
    "EraBoundary",
    "StratumKey",
    "TrendStratum",
    "VolBand",
    "classify_session",
    "classify_vix_band",
    "era_for_date",
    "in_extreme_vol_overlay",
    "is_covid_2020_overlay",
    "trend_stratum_from_lens_token",
    "validate_era_boundaries",
]

#: The taxonomy schema version. ANY boundary/threshold/vocabulary change
#: (VIX band edges, era boundary dates/labels, crisis-overlay windows,
#: enum members) MUST bump this — a consuming gate folds the version into
#: its config so taxonomy drift re-keys instead of silently re-labeling.
TAXONOMY_VERSION: Final[int] = 1


# ── VOL axis ─────────────────────────────────────────────────────────────────


class VolBand(Enum):
    """The volatility stratum of a session, from its India-VIX close."""

    LOW = "low"
    MID = "mid"
    HIGH = "high"
    EXTREME = "extreme"


#: FIXED VIX band edges (half-open ``[lo, hi)`` intervals): LOW ``< 13``,
#: MID ``[13, 17)``, HIGH ``[17, 25)``, EXTREME ``>= 25``. Deliberately
#: NOT winner-relative — a labeling convention pending operator
#: calibration, never derived from any strategy's own results.
VIX_LOW_MAX: Final[float] = 13.0
VIX_MID_MAX: Final[float] = 17.0
VIX_HIGH_MAX: Final[float] = 25.0


def classify_vix_band(vix_close: float) -> VolBand:
    """Classify a session's India-VIX close into its :class:`VolBand`.

    Parameters
    ----------
    vix_close : float
        The session's VIX close (an index level, never money).

    Returns
    -------
    VolBand
        ``LOW`` for ``< 13``, ``MID`` for ``[13, 17)``, ``HIGH`` for
        ``[17, 25)``, ``EXTREME`` for ``>= 25``.

    Raises
    ------
    ValueError
        On a non-finite (nan/inf) or non-positive VIX value — a band is
        NEVER fabricated for junk input (Golden Rule #1); callers with a
        missing VIX pass ``None`` to :func:`classify_session` instead,
        which abstains honestly.
    """
    if not math.isfinite(vix_close) or vix_close <= 0.0:
        msg = (
            f"classify_vix_band: vix_close must be a finite positive index "
            f"level, got {vix_close!r} — a band is never fabricated"
        )
        raise ValueError(msg)
    if vix_close < VIX_LOW_MAX:
        return VolBand.LOW
    if vix_close < VIX_MID_MAX:
        return VolBand.MID
    if vix_close < VIX_HIGH_MAX:
        return VolBand.HIGH
    return VolBand.EXTREME


# ── TREND axis ───────────────────────────────────────────────────────────────


class TrendStratum(Enum):
    """The trend stratum of a session, from the session-lens vocabulary."""

    UP = "up"
    DOWN = "down"
    CHOP = "chop"
    UNKNOWN = "unknown"


def trend_stratum_from_lens_token(token: str | None) -> TrendStratum:
    """Map a session-lens trend token onto its :class:`TrendStratum`.

    The ONE producer of the input vocabulary is
    ``brutex/engine/session_lens.py`` ``build_session_lens`` (its
    ``TrendRegime`` enum over ``trading_core.MarketStructureState`` with
    the production fractal half-window ``k = 2``); this leaf consumes the
    enum's VALUE tokens (``"up"`` / ``"down"`` / ``"sideways"``) plus the
    combo-aggregates ``TREND_UNKNOWN`` bucket (``"unknown"``) — never a
    second classifier (§11 COMMON by consumption).

    Parameters
    ----------
    token : str | None
        A lens token; ``None`` means the lens could not label the session.

    Returns
    -------
    TrendStratum
        ``"up"`` → UP, ``"down"`` → DOWN, ``"sideways"`` → CHOP;
        ``None`` / ``"unknown"`` / any unrecognized token → UNKNOWN (an
        honest bucket, never a guess and never a raise — a future lens
        vocabulary extension degrades to UNKNOWN instead of crashing).
    """
    if token == "up":  # noqa: S105  # nosec B105 — lens trend token, not a secret
        return TrendStratum.UP
    if token == "down":  # noqa: S105  # nosec B105 — lens trend token, not a secret
        return TrendStratum.DOWN
    if token == "sideways":  # noqa: S105  # nosec B105 — lens trend token, not a secret
        return TrendStratum.CHOP
    return TrendStratum.UNKNOWN


# ── STRUCTURAL-ERA axis ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EraBoundary:
    """One dated structural-era row (the ``RegimeRow`` refusal-precedent
    shape — ``brutex/costs/constants.py``, DEC-ETC-REGIME-HISTORY-001).

    Parameters
    ----------
    start : date
        The era's effective date (inclusive).
    label : str
        The era's stable partition token (unique across the table).
    source : str
        The citation (verified row) or the honest citation-gap stamp
        (an unverified row MUST carry the literal token ``"UNVERIFIED"``
        — enforced by :func:`validate_era_boundaries`). Never blank.
    verified : bool
        Whether ``source`` is a citation-grounded fact. An unverified
        boundary is still USABLE for labeling (unlike a ``RegimeRow``
        refusal window, an era label is a partition token, not a charge
        — nothing is priced off it), but the gap is stamped explicitly.
    """

    start: date
    label: str
    source: str
    verified: bool


#: The structural-era table, ordered strictly ascending by ``start``.
#: Import-time validated by :func:`validate_era_boundaries` below.
ERA_BOUNDARIES: Final[tuple[EraBoundary, ...]] = (
    EraBoundary(
        date(2020, 1, 1),
        "pre_peak_margin",
        "window floor (GROWW_HISTORY_FLOOR); era opens the modeled span",
        True,
    ),
    EraBoundary(
        date(2021, 9, 1),
        "post_peak_margin_full",
        "UNVERIFIED — secondary-corroborated "
        "(SEBI/HO/MRD2/DCAP/CIR/P/2020/127, phase-in 25% Dec-2020 -> "
        "100% 2021-09-01); primary circular not retrieved",
        False,
    ),
    EraBoundary(
        date(2024, 11, 14),
        "post_weekly_rationalization",
        "SEBI weekly-expiry rationalization; BANKNIFTY weekly discontinued "
        "after EOD 2024-11-13 — mirrors brutex/options/expiry_calendar.py "
        "encoded regime (in-repo verified)",
        True,
    ),
)


def era_for_date(d: date) -> EraBoundary:
    """Return the structural era in force on *d*.

    O(log N) bisect over the era starts: the era is the LATEST boundary
    whose ``start <= d``. A date BEFORE the first boundary returns the
    FIRST era — a documented TOTAL-function choice (an era label is a
    partition token, not a priced fact, so totality beats a refusal here;
    the modeled span opens at the first boundary anyway per the window
    floor).

    Parameters
    ----------
    d : date
        The session date.

    Returns
    -------
    EraBoundary
        The era row in force on *d*.
    """
    idx = bisect_right(_ERA_STARTS, d)
    if idx == 0:
        return ERA_BOUNDARIES[0]
    return ERA_BOUNDARIES[idx - 1]


# ── CRISIS overlays (annotations, NOT partition cells) ───────────────────────

#: The COVID-2020 crisis overlay window, INCLUSIVE on both ends — a chosen
#: labeling convention (crash onset through the initial recovery), NOT an
#: official crisis declaration or regulatory window.
COVID_2020_START: Final[date] = date(2020, 2, 15)
COVID_2020_END: Final[date] = date(2020, 6, 30)


def is_covid_2020_overlay(d: date) -> bool:
    """Whether *d* falls in the COVID-2020 overlay window (both ends
    inclusive: ``COVID_2020_START <= d <= COVID_2020_END``)."""
    return COVID_2020_START <= d <= COVID_2020_END


#: The extreme-volatility overlay opens at this date (a labeling
#: convention marking the modern high-vol episode span).
EXTREME_VOL_OVERLAY_START: Final[date] = date(2024, 1, 1)


def in_extreme_vol_overlay(d: date, vix_close: float | None) -> bool | None:
    """Whether the session is in the extreme-volatility overlay.

    Parameters
    ----------
    d : date
        The session date.
    vix_close : float | None
        The session's VIX close, or ``None`` when unavailable.

    Returns
    -------
    bool | None
        ``None`` when *vix_close* is ``None`` — an HONEST abstain, never
        a fabricated verdict. Otherwise ``True`` iff
        ``d >= EXTREME_VOL_OVERLAY_START`` AND the VIX band is
        :attr:`VolBand.EXTREME`.

    Raises
    ------
    ValueError
        Propagated from :func:`classify_vix_band` on a junk (non-finite
        or non-positive) *vix_close*.
    """
    if vix_close is None:
        return None
    return d >= EXTREME_VOL_OVERLAY_START and classify_vix_band(vix_close) is VolBand.EXTREME


# ── Composite ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StratumKey:
    """The composite stratum of one session: (vol, trend, era).

    ``vol`` is ``None`` when the session's VIX close was unavailable —
    the honest abstain, never a fabricated band. Crisis overlays are
    deliberately NOT part of the key (they annotate, never partition).
    """

    vol: VolBand | None
    trend: TrendStratum
    era_label: str


def classify_session(
    d: date,
    *,
    vix_close: float | None,
    trend_token: str | None,
) -> StratumKey:
    """Classify one session into its composite :class:`StratumKey`.

    Parameters
    ----------
    d : date
        The session date.
    vix_close : float | None
        The session's India-VIX close; ``None`` abstains the vol axis
        (``StratumKey.vol is None``) — never a fabricated band.
    trend_token : str | None
        The session-lens trend token (see
        :func:`trend_stratum_from_lens_token`).

    Returns
    -------
    StratumKey
        The session's composite stratum.

    Raises
    ------
    ValueError
        Propagated from :func:`classify_vix_band` on a junk (non-finite
        or non-positive) *vix_close* — junk is refused, never binned.
    """
    vol = None if vix_close is None else classify_vix_band(vix_close)
    return StratumKey(
        vol=vol,
        trend=trend_stratum_from_lens_token(trend_token),
        era_label=era_for_date(d).label,
    )


# ── Table validation (import-time, fail-loud) ────────────────────────────────


def validate_era_boundaries(rows: Iterable[EraBoundary]) -> None:
    """Fail-loud structural validation of an era table (the
    ``validate_regime_table`` mirror), applied ONCE at import to
    :data:`ERA_BOUNDARIES`.

    Parameters
    ----------
    rows : Iterable[EraBoundary]
        The candidate table.

    Raises
    ------
    ValueError
        If the table is empty; the ``start`` dates are not STRICTLY
        ascending (one check covers unsorted AND duplicate-start rows);
        any label is duplicated; the first row does not start at
        ``date(2020, 1, 1)`` (the window floor law); or an unverified
        row's ``source`` lacks the literal ``"UNVERIFIED"`` stamp (the
        RegimeRow honesty law — a citation gap is never silent).
    """
    table = tuple(rows)
    if not table:
        msg = "era table is empty"
        raise ValueError(msg)
    if table[0].start != date(2020, 1, 1):
        msg = f"era table must open at the window floor date(2020, 1, 1) — got {table[0].start}"
        raise ValueError(msg)
    for prev, cur in pairwise(table):
        if cur.start <= prev.start:
            msg = (
                f"era table: row starts must be strictly ascending — got "
                f"{prev.start} then {cur.start}"
            )
            raise ValueError(msg)
    labels = [row.label for row in table]
    if len(set(labels)) != len(labels):
        msg = f"era table: labels must be unique — got {labels!r}"
        raise ValueError(msg)
    for row in table:
        if not row.verified and "UNVERIFIED" not in row.source:
            msg = (
                f"era table: unverified row {row.label!r} must stamp the "
                f"literal 'UNVERIFIED' in its source — got {row.source!r}"
            )
            raise ValueError(msg)


validate_era_boundaries(ERA_BOUNDARIES)

#: Bisect keys for :func:`era_for_date` (built AFTER validation).
_ERA_STARTS: Final[tuple[date, ...]] = tuple(row.start for row in ERA_BOUNDARIES)
