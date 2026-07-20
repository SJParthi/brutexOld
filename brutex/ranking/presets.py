"""Named ranking presets over joined (row ⋈ aggregate) rank records.

DEC-SWEEP-RESULTS-DEPTH-001 (WP3). R30's operator law — "least pain wins" —
plus the one-click alternates, in ONE home (CLAUDE.md §11 COMMON): the global
top-100 builder (:mod:`brutex.engine.top100_artifact`) delegates its whole
ordering law here, so a preset can never fork between the artifact build and
any future re-ranking surface (P4's screener).

## The R30 total order (as amended by DEC-RANK-DD-BAND-ORDER-001)

Default ``min_dd_first``: smallest ``max_drawdown_pct`` **ε-BAND** first
(LANE W1 item 5 — the DD is quantized into :data:`DD_BAND_PCT`-wide bands via
:func:`dd_band_index`, killing the 2bp-DD-beats-58×-P&L pathology of the old
strict-DD first key; the band index, not the raw DD, is key 1) → composite
score (full precision, never rounded) descending as the tiebreak — a ``None``
composite ranks AFTER every real one, never coerced to a fake number →
fewest ``max_consecutive_losses`` → ``run_id`` ASC (the store's tie law).
BOUNDARY RULE (documented, deterministic): the band is
``math.floor(dd / DD_BAND_PCT)`` — a DD exactly on a band edge (e.g. 0.5 at
band width 0.5) joins the UPPER band index via floor. The quantizer is
bit-mirrored by the born-Rust batch kernel ``fno_math.rank_dd_band_indices``
(same IEEE division + floor — drift-pinned by the parity vectors in
``tests/unit/test_engine_rust_rank_select.py``); the per-record sort key
computes the same floor locally because this module is a stdlib-only leaf (a
per-record scalar FFI call inside a sort key would be a pessimization — the
recon brief's recommended shape, disclosed in MIGRATION.md #2062).
Alternates ``composite`` / ``profit_factor`` / ``win_rate`` / ``calmar`` /
``growth`` (half-Kelly log growth, DEC-METRICS-KELLY-GROWTH-001 — a ``None``
growth ranks after every scored one, the composite None-after-scored idiom):
each DESCENDING on the RAW uncoerced value with the same ``run_id``-ASC
stable pre-sort — the exact ``ResultsStore.top_k`` two-pass law
(recon-ranking §3), so every preset is a strict, discovery-order-free total
order (property-pinned). ``win_rate_safe``
(DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) is the second MULTI-part
ascending preset (the ``min_dd_first`` in-tuple sign-flip idiom): win rate
DESC → SL-hit rate ASC (a ``None`` rate — a pre-extension store row — ranks
AFTER every concrete one within an equal win rate, the None-presence-flag
law) → composite DESC (``None`` after scored) → ``run_id`` ASC.
The Stage-C recency alternates (DEC-METRICS-TRAILING-WINDOW-001):
``recent`` (trailing 24-month Sharpe DESC, None-after-scored) and
``recent_decay`` (whole-history decay-weighted net P&L DESC, same law) —
both appended LAST.

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

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Mapping, Sequence

_LOG = structlog.get_logger("brutex.ranking.presets")

__all__ = [
    "DD_BAND_PCT",
    "DEFAULT_PRESET",
    "PRESETS",
    "PRESET_MIN_DD_FIRST",
    "PRESET_NAMES",
    "PRESET_RECENT",
    "PRESET_RECENT_DECAY",
    "PRESET_WIN_RATE_SAFE",
    "RankRecord",
    "RankingPreset",
    "dd_band_index",
    "min_dd_first_key",
    "resolve_preset",
    "sort_by_preset",
    "win_rate_safe_key",
]

#: The R30 default preset token + the one-click alternates, in display order.
PRESET_MIN_DD_FIRST: Final[str] = "min_dd_first"
DEFAULT_PRESET: Final[str] = PRESET_MIN_DD_FIRST

#: The Stage-B ``win_rate_safe`` preset token
#: (DEC-METRICS-LOSSRATE-FIRSTCLASS-001) — appended LAST to :data:`PRESETS`.
PRESET_WIN_RATE_SAFE: Final[str] = "win_rate_safe"

#: The Stage-C recency preset tokens (DEC-METRICS-TRAILING-WINDOW-001) —
#: appended LAST to :data:`PRESETS`, after ``win_rate_safe``. ``recent``
#: orders on the trailing-window Sharpe; ``recent_decay`` on the
#: whole-history decay-weighted net P&L (the lane's reading of the map's
#: "decay-weighted composite variant" — a distinct preset over the persisted
#: decay composite, named in the DEC).
PRESET_RECENT: Final[str] = "recent"
PRESET_RECENT_DECAY: Final[str] = "recent_decay"

#: The min_dd_first DD ε-band width in max_drawdown_pct percentage points
#: (DEC-RANK-DD-BAND-ORDER-001, LANE W1 item 5). The design map's example
#: value ("e.g. 0.5% steps, operator constant") adopted as the W1 DEFAULT —
#: DISCLOSED as standing in for the un-ruled operator constant
#: (MIGRATION.md #2062). Within one band the composite decides; across bands
#: the smaller band still wins ("least pain" preserved at band granularity).
DD_BAND_PCT: Final[float] = 0.5


def dd_band_index(max_drawdown_pct: float, *, band_pct: float = DD_BAND_PCT) -> int:
    """Quantize a max-drawdown %% into its ε-band index — ``floor(dd / band)``.

    Deterministic pure function; the boundary rule is FLOOR (a DD exactly on
    a band edge joins the UPPER band index — module docstring). Bit-mirrored
    by the born-Rust batch kernel ``fno_math.rank_dd_band_indices``
    (drift-pinned). A non-finite DD is refused loudly (``ResultRow``
    guarantees finite metrics; a hand-built record must not silently sort).
    """
    if not math.isfinite(max_drawdown_pct):
        raise ValueError(
            f"dd_band_index: max_drawdown_pct must be finite, got {max_drawdown_pct!r}"
        )
    return math.floor(max_drawdown_pct / band_pct)


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
    #: The persisted half-Kelly log-growth figure feeding the ``growth``
    #: preset (DEC-METRICS-KELLY-GROWTH-001). ``None`` = honestly unscored
    #: (a pre-extension store row) — ranks AFTER every scored record under
    #: the ``_growth_key`` presence flag, exactly like an unscored
    #: composite. Defaulted so pre-extension record builders stay valid.
    log_growth_at_half_kelly: float | None = None
    #: The persisted SL-hit rate feeding the ``win_rate_safe`` preset
    #: (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B). ``None`` = honestly
    #: unknown (a pre-extension store row) — ranks AFTER every concrete rate
    #: within an equal win rate under the :func:`win_rate_safe_key` presence
    #: flag, never coerced to a fake 0.0 (which would rank a pre-extension
    #: row as "never hits SL"). Defaulted-LAST so pre-extension record
    #: builders stay valid.
    sl_hit_rate: float | None = None
    #: The persisted trailing-window Sharpe feeding the ``recent`` preset
    #: (DEC-METRICS-TRAILING-WINDOW-001). ``None`` = honestly unscored (a
    #: pre-extension store row, an anchor-less run, or a 24-month window
    #: with zero trades) — ranks AFTER every scored record under the
    #: presence flag, never coerced. Defaulted-LAST.
    trailing_sharpe_ratio: float | None = None
    #: The persisted decay-weighted net P&L feeding the ``recent_decay``
    #: preset (DEC-METRICS-TRAILING-WINDOW-001). ``None`` = honestly
    #: unscored (a pre-extension store row or an anchor-less run) — ranks
    #: AFTER every scored record. Defaulted-LAST.
    decay_weighted_net_paisa: float | None = None
    #: ALPHA vs buy-&-hold, percent (DEC-ALPHA-BUYHOLD-METRIC-001) — an
    #: ARTIFACT-level display metric, not persisted on ``ResultRow``.
    #: Defaulted LAST so every existing construction site is byte-identical;
    #: ``None`` == honestly uncomputable (no baseline for the window) and
    #: ranks AFTER every real value under the ``alpha`` preset (the
    #: None-after-scored law), never coerced to a fake number.
    alpha_pct: float | None = None


#: A preset sort key — the 4-part BANDED min-DD ascending key
#: (DEC-RANK-DD-BAND-ORDER-001 — band index first), the 5-part
#: win-rate-safe ascending key, or the ``(value_present, raw_value)``
#: descending key of the single-metric alternates.
type PresetKey = (
    tuple[int, int, float, int] | tuple[int, float] | tuple[float, int, float, int, float]
)


def min_dd_first_key(record: RankRecord) -> tuple[int, int, float, int]:
    """The R30 ``min_dd_first`` sort key (ascending sort) — DD ε-BANDED.

    DEC-RANK-DD-BAND-ORDER-001 (LANE W1 item 5): key 1 is the
    :func:`dd_band_index` of the stored max drawdown %% (bands of
    :data:`DD_BAND_PCT` points — a 2bp DD edge no longer outranks a 58× P&L
    edge); then composite score DESC within the band (a ``None`` composite
    ranks AFTER every real one — an unscored combo never outranks a scored
    one); then fewest consecutive losses (RETAINED from the pre-band law).
    Ties fall through to the stable ``run_id``-ASC pre-sort (RETAINED).
    """
    composite = record.composite_score
    return (
        dd_band_index(record.max_drawdown_pct),
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


def _growth_key(record: RankRecord) -> tuple[int, float]:
    """``(value_present, log_growth_at_half_kelly)`` — the ``growth`` preset key.

    The ``_composite_key`` None-after-scored idiom (DEC-METRICS-KELLY-GROWTH-001):
    a ``None`` log-growth (a pre-extension store row — honestly unscored)
    sorts LAST under ``reverse=True`` via the presence flag, never coerced to
    a fake number; ties fall through to the stable ``run_id``-ASC pre-sort.
    """
    growth = record.log_growth_at_half_kelly
    if growth is None:
        return (0, 0.0)
    return (1, growth)


def win_rate_safe_key(record: RankRecord) -> tuple[float, int, float, int, float]:
    """The Stage-B ``win_rate_safe`` sort key (ascending sort).

    DEC-METRICS-LOSSRATE-FIRSTCLASS-001: highest win rate first (in-tuple
    sign flip — the :func:`min_dd_first_key` idiom, ONE ``reverse`` per
    preset); then SL-hit rate ASCENDING (fewest stop-loss exits wins the
    win-rate tie — a ``None`` rate ranks AFTER every concrete one via the
    presence flag: an unknown-exit-quality row never outranks a measured
    one); then composite score DESC (``None`` after scored — the
    :func:`min_dd_first_key` composite arms verbatim). Ties fall through to
    the stable ``run_id``-ASC pre-sort.
    """
    sl_hit_rate = record.sl_hit_rate
    composite = record.composite_score
    return (
        -record.win_rate,
        0 if sl_hit_rate is not None else 1,
        sl_hit_rate if sl_hit_rate is not None else 0.0,
        0 if composite is not None else 1,
        -(composite if composite is not None else 0.0),
    )


def _trailing_sharpe_key(record: RankRecord) -> tuple[int, float]:
    """``(value_present, trailing_sharpe_ratio)`` — the ``recent`` preset key.

    The ``_composite_key`` None-after-scored idiom
    (DEC-METRICS-TRAILING-WINDOW-001): a ``None`` trailing Sharpe (a
    pre-extension store row, an anchor-less run, or a trailing window with
    zero trades — all honestly unscored) sorts LAST under ``reverse=True``
    via the presence flag, never coerced to a fake number; ties fall through
    to the stable ``run_id``-ASC pre-sort.
    """
    trailing_sharpe = record.trailing_sharpe_ratio
    if trailing_sharpe is None:
        return (0, 0.0)
    return (1, trailing_sharpe)


def _decay_net_key(record: RankRecord) -> tuple[int, float]:
    """``(value_present, decay_weighted_net_paisa)`` — the ``recent_decay`` key.

    Same None-after-scored law (DEC-METRICS-TRAILING-WINDOW-001): a ``None``
    decay composite (a pre-extension store row or an anchor-less run) sorts
    LAST under ``reverse=True``; ties fall through to ``run_id``-ASC.
    """
    decay_net = record.decay_weighted_net_paisa
    if decay_net is None:
        return (0, 0.0)
    return (1, decay_net)


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


#: The named preset table (R30): the default + the one-click alternates,
#: in display order. Keys are the wire tokens the CLI/dashboard pass around.
PRESETS: Final[Mapping[str, RankingPreset]] = {
    PRESET_MIN_DD_FIRST: RankingPreset(
        name=PRESET_MIN_DD_FIRST,
        describe=(
            "least pain wins — smallest max-drawdown ε-band first (0.5% "
            "bands), composite score within band, fewest consecutive losses"
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
    "growth": RankingPreset(
        name="growth",
        describe=("half-Kelly log growth, descending (unscored/pre-extension rows rank last)"),
        reverse=True,
        key=_growth_key,
    ),
    PRESET_WIN_RATE_SAFE: RankingPreset(
        name=PRESET_WIN_RATE_SAFE,
        describe=(
            "win rate first, fewest stop-loss exits break the tie (unknown "
            "SL-hit rates rank last within a win rate), composite tiebreak"
        ),
        reverse=False,
        key=win_rate_safe_key,
    ),
    PRESET_RECENT: RankingPreset(
        name=PRESET_RECENT,
        describe=(
            "trailing 24-month Sharpe, descending (unscored / no-recent-trade rows rank last)"
        ),
        reverse=True,
        key=_trailing_sharpe_key,
    ),
    PRESET_RECENT_DECAY: RankingPreset(
        name=PRESET_RECENT_DECAY,
        describe=(
            "decay-weighted net P&L (12-month half-life over the whole "
            "history), descending (unscored rows rank last)"
        ),
        reverse=True,
        key=_decay_net_key,
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
