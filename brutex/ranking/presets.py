"""Named ranking presets over joined (row ⋈ aggregate) rank records.

DEC-SWEEP-RESULTS-DEPTH-001 (WP3). R30's operator law — "least pain wins" —
plus the one-click alternates, in ONE home (CLAUDE.md §11 COMMON): the global
top-100 builder (:mod:`brutex.engine.top100_artifact`) delegates its whole
ordering law here, so a preset can never fork between the artifact build and
any future re-ranking surface (P4's screener).

## The R30 total order

Default ``min_dd_first``: smallest ``max_drawdown_pct`` FIRST → composite
score (full precision, never rounded) descending as the tiebreak — a ``None``
composite ranks AFTER every real one, never coerced to a fake number →
fewest ``max_consecutive_losses`` → ``run_id`` ASC (the store's tie law).
Alternates ``composite`` / ``profit_factor`` / ``win_rate`` / ``calmar``:
each DESCENDING on the RAW uncoerced value with the same ``run_id``-ASC
stable pre-sort — the exact ``ResultsStore.top_k`` two-pass law
(recon-ranking §3), so every preset is a strict, discovery-order-free total
order (property-pinned).

## Inputs

:class:`RankRecord` is the FLAT projection of the joined
``(ResultRow, ComboAggregate)`` pair — the row's persisted full-precision
metrics plus the aggregate's recomputed depth stats. The projection is the
CALLER's job (the builder knows its own record shapes); this module stays a
stdlib-only leaf with zero engine imports (no import cycle —
``brutex.engine`` already imports ``brutex.ranking``).

Unknown preset tokens degrade to the default, logged (the /results
``_resolve_rank_by`` idiom) — a hostile or stale token can never 500 a
build. Gates are NOT applied here (R30: "ranking is post-gate"; the gate
law lives with the builder).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Mapping, Sequence

_LOG = structlog.get_logger("brutex.ranking.presets")

__all__ = [
    "DEFAULT_PRESET",
    "PRESETS",
    "PRESET_MIN_DD_FIRST",
    "PRESET_NAMES",
    "RankRecord",
    "RankingPreset",
    "min_dd_first_key",
    "resolve_preset",
    "sort_by_preset",
]

#: The R30 default preset token + the one-click alternates, in display order.
PRESET_MIN_DD_FIRST: Final[str] = "min_dd_first"
DEFAULT_PRESET: Final[str] = PRESET_MIN_DD_FIRST


@dataclass(frozen=True, slots=True)
class RankRecord:
    """The flat rank inputs of ONE joined (ResultRow ⋈ ComboAggregate) finalist.

    ``max_drawdown_pct`` / ``profit_factor`` / ``win_rate`` / ``calmar_ratio``
    come from the persisted row (full precision, never rounded — ranking
    integrity, CLAUDE.md §4); ``composite_score`` / ``max_consecutive_losses``
    from the recomputed aggregate (``None`` composite == honestly unscored).
    """

    run_id: str
    max_drawdown_pct: float
    composite_score: float | None
    max_consecutive_losses: int
    profit_factor: float
    win_rate: float
    calmar_ratio: float
    #: ALPHA vs buy-&-hold, percent (DEC-ALPHA-BUYHOLD-METRIC-001) — an
    #: ARTIFACT-level display metric, not persisted on ``ResultRow``.
    #: Defaulted LAST so every existing construction site is byte-identical;
    #: ``None`` == honestly uncomputable (no baseline for the window) and
    #: ranks AFTER every real value under the ``alpha`` preset (the
    #: None-after-scored law), never coerced to a fake number.
    alpha_pct: float | None = None


#: A preset sort key — either the 4-part min-DD ascending key or the
#: ``(value_present, raw_value)`` descending key of the alternates.
type PresetKey = tuple[float, int, float, int] | tuple[int, float]


def min_dd_first_key(record: RankRecord) -> tuple[float, int, float, int]:
    """The R30 ``min_dd_first`` sort key (ascending sort).

    Smallest stored max drawdown % first; then composite score DESC (a
    ``None`` composite ranks AFTER every real one — an unscored combo never
    outranks a scored one); then fewest consecutive losses. Ties fall through
    to the stable ``run_id``-ASC pre-sort.
    """
    composite = record.composite_score
    return (
        record.max_drawdown_pct,
        0 if composite is not None else 1,
        -(composite if composite is not None else 0.0),
        record.max_consecutive_losses,
    )


def _composite_key(record: RankRecord) -> tuple[int, float]:
    """``(value_present, raw_value)`` — a ``None`` composite sorts LAST under
    ``reverse=True`` via the presence flag, never coerced to a fake number."""
    composite = record.composite_score
    if composite is None:
        return (0, 0.0)
    return (1, composite)


def _profit_factor_key(record: RankRecord) -> tuple[int, float]:
    """Raw persisted profit factor (never ``None`` on a persisted row)."""
    return (1, record.profit_factor)


def _win_rate_key(record: RankRecord) -> tuple[int, float]:
    """Raw persisted win rate (fraction, full precision)."""
    return (1, record.win_rate)


def _calmar_key(record: RankRecord) -> tuple[int, float]:
    """Raw persisted Calmar ratio (full precision)."""
    return (1, record.calmar_ratio)


def _alpha_key(record: RankRecord) -> tuple[int, float]:
    """``(value_present, raw_value)`` — the alpha-vs-buy-&-hold axis
    (DEC-ALPHA-BUYHOLD-METRIC-001). A ``None`` alpha (no baseline for the
    window) sorts LAST under ``reverse=True`` via the presence flag, never
    coerced to a fake number (the ``_composite_key`` law, verbatim)."""
    alpha = record.alpha_pct
    if alpha is None:
        return (0, 0.0)
    return (1, alpha)


@dataclass(frozen=True, slots=True)
class RankingPreset:
    """One named ranking law: a sort key + its direction.

    ``reverse=False`` sorts the key ASCENDING (the min-DD law);
    ``reverse=True`` sorts DESCENDING on the raw value (the alternates).
    """

    name: str
    describe: str
    reverse: bool
    key: Callable[[RankRecord], PresetKey]

    def sort_records(self, records: Sequence[RankRecord]) -> tuple[RankRecord, ...]:
        """Order *records* under this preset — a strict total order."""
        return sort_by_preset(records, preset=self, record_of=lambda record: record)


def sort_by_preset[T](
    items: Sequence[T], *, preset: RankingPreset, record_of: Callable[[T], RankRecord]
) -> tuple[T, ...]:
    """The ONE two-pass ordering law, generic over the caller's item shape.

    The exact ``ResultsStore.top_k`` law: a stable ``run_id``-ASC pre-sort,
    then ONE stable sort on the preset key — identical output for any input
    permutation (property-pinned). ``record_of`` projects each item onto its
    :class:`RankRecord`; the projection runs ONCE per item (O(n) projections
    + O(n log n) sort — never re-projected per comparison).
    """
    keyed = sorted(
        ((record_of(item), item) for item in items),
        key=lambda pair: pair[0].run_id,
    )
    keyed.sort(key=lambda pair: preset.key(pair[0]), reverse=preset.reverse)
    return tuple(item for _record, item in keyed)


#: The named preset table (R30): the default + the four one-click alternates,
#: in display order. Keys are the wire tokens the CLI/dashboard pass around.
PRESETS: Final[Mapping[str, RankingPreset]] = {
    PRESET_MIN_DD_FIRST: RankingPreset(
        name=PRESET_MIN_DD_FIRST,
        describe=(
            "least pain wins — smallest max drawdown first, composite score "
            "tiebreak, fewest consecutive losses"
        ),
        reverse=False,
        key=min_dd_first_key,
    ),
    "composite": RankingPreset(
        name="composite",
        describe="7-part composite score, descending (unscored combos rank last)",
        reverse=True,
        key=_composite_key,
    ),
    "profit_factor": RankingPreset(
        name="profit_factor",
        describe="profit factor, descending",
        reverse=True,
        key=_profit_factor_key,
    ),
    "win_rate": RankingPreset(
        name="win_rate",
        describe="win rate, descending",
        reverse=True,
        key=_win_rate_key,
    ),
    "calmar": RankingPreset(
        name="calmar",
        describe="Calmar ratio, descending",
        reverse=True,
        key=_calmar_key,
    ),
    # DEC-ALPHA-BUYHOLD-METRIC-001 — APPENDED (additive; the default token
    # and every existing preset's position are byte-identical).
    "alpha": RankingPreset(
        name="alpha",
        describe=(
            "alpha vs buy-&-hold, descending (strategy return % minus the "
            "instrument's buy-&-hold return % over the same window; "
            "uncomputable rows rank last)"
        ),
        reverse=True,
        key=_alpha_key,
    ),
}

#: The preset tokens in display order (the artifact's advertised vocabulary).
PRESET_NAMES: Final[tuple[str, ...]] = tuple(PRESETS)


def resolve_preset(name: str) -> RankingPreset:
    """Resolve a preset token; unknown/blank degrades to the default, logged.

    The /results ``_resolve_rank_by`` idiom — a hostile or stale token can
    never break a build; the degradation is visible
    (``ranking.presets.unknown_preset``).
    """
    preset = PRESETS.get(name)
    if preset is not None:
        return preset
    _LOG.warning(
        "ranking.presets.unknown_preset",
        requested=name,
        resolved=DEFAULT_PRESET,
        known=PRESET_NAMES,
    )
    return PRESETS[DEFAULT_PRESET]
