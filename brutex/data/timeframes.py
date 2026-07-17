"""Track 1 timeframe canonical registry — all 21 timeframes.

Per `docs/TRACK1_BACKTEST_SPEC.md` §Workload Math:
    "21 timeframes (1m base + 19 derived intraday + 1d)"

The brute-force engine evaluates strategies at all 21 of these
timeframes. Bar lookup is O(1) per TF (per CLAUDE.md §2 #5).

## Composition

* 15 minute-level intraday TFs: 1m, 2m, ... 15m
* 5 multi-minute intraday TFs:  30m, 60m (=1h), 120m (=2h), 180m (=3h), 240m (=4h)
* 1 daily TF: 1d
* Total: 21

## Anchor convention

All resampling uses **left-closed, left-labeled** buckets anchored at
09:15 IST (session open). Example: 5m bar at 09:15 IST covers
[09:15:00, 09:20:00).

## The seconds-keyed registry (DEC-TF-REGISTRY-DYNAMIC-001)

:data:`ALL_TFS` is THE timeframe registry — a tuple of :class:`TfSpec`
entries keyed in SECONDS (``duration_s``), each carrying its bucket
anchor (:class:`TfAnchor`), its short label (``"5m"``) and its lake
partition label (``"5minute"``). The legacy minutes-keyed vocabulary
(:data:`ALL_TF_MINUTES` / :data:`TF_LABELS` /
:data:`LAKE_INTERVAL_LABELS`) is a DERIVED VIEW over the registry
(whole-minute specs only) with byte-identical values, so every
pre-registry consumer is unchanged. ``brutex.data.partition``'s
``VALID_INTERVALS`` and ``brutex.feed.resampler``'s
``INTERVAL_SECONDS`` derive from the registry too — a newly registered
timeframe propagates automatically.

HONEST GATE (Golden Rule #10): sub-minute ``TfSpec``s (a 1-second or
30-second timeframe) are accepted by the TYPE — the framework is
seconds-generic — but the DEFAULT registry contains ONLY the 21
whole-minute timeframes, because NO sub-minute production feed exists
(Groww serves 1-minute at finest; a 1-second feed like GDFL is
unbuilt / forward-capture-only per docs/vendors/gdfl/). Registering a
sub-minute timeframe requires its own DEC plus a real sub-minute feed;
nothing here claims sub-minute DATA works today.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

# 15 minute-by-minute intraday timeframes (1m to 15m).
INTRADAY_MINUTE_TFS: Final[tuple[int, ...]] = (
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
)

# 5 multi-minute intraday timeframes.
INTRADAY_MULTIMINUTE_TFS: Final[tuple[int, ...]] = (
    30,
    60,
    120,
    180,
    240,
)

# 1 daily timeframe (in minutes — 6h 15m session).
DAILY_TF_MINUTES: Final[int] = 375

#: Track 1 timeframe count per spec (1m base + 19 derived intraday + 1d).
EXPECTED_TF_COUNT: Final[int] = 21

#: Seconds per minute — the base granularity of every DEFAULT timeframe
#: (the lake's fetched base is 1-minute bars).
_MINUTE_S: Final[int] = 60


@unique
class TfAnchor(Enum):
    """Bucket-grid anchor family for a timeframe (DEC-TF-REGISTRY-DYNAMIC-001).

    * ``SESSION_OPEN`` — intraday grid anchored at the 09:15 IST session
      open: ``bucket = open + floor((ts - open) / duration) * duration``.
    * ``IST_DAY`` — one bucket per IST trading day, labeled at the 09:15
      IST session open (the daily timeframe).
    """

    SESSION_OPEN = "session_open"
    IST_DAY = "ist_day"


@dataclass(frozen=True, slots=True)
class TfSpec:
    """One timeframe's registry entry — seconds-keyed (DEC-TF-REGISTRY-DYNAMIC-001).

    Attributes
    ----------
    label
        The short config label (e.g. ``"5m"``, ``"1d"``); non-empty.
    duration_s
        Bucket length in seconds (``>= 1``). The daily timeframe carries
        the 375-minute TRADING session (22_500 s), never 86_400 wall-clock
        seconds (the ``trading_minutes_for_lake_interval`` law).
    base_granularity_s
        The finest source-bar granularity this timeframe derives from
        (``60`` for every default timeframe — the fetched 1-minute base);
        must divide ``duration_s`` exactly.
    anchor
        The bucket-grid anchor family (:class:`TfAnchor`).
    lake_label
        The lake partition directory label (e.g. ``"5minute"``); non-empty.

    Raises
    ------
    ValueError
        On any invalid field (fail-loud construction).
    """

    label: str
    duration_s: int
    base_granularity_s: int
    anchor: TfAnchor
    lake_label: str

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("TfSpec.label must be non-empty")
        if not self.lake_label:
            raise ValueError("TfSpec.lake_label must be non-empty")
        if not isinstance(self.anchor, TfAnchor):
            raise ValueError(f"TfSpec.anchor must be a TfAnchor member, got {self.anchor!r}")
        if self.duration_s < 1:
            raise ValueError(f"TfSpec.duration_s must be >= 1, got {self.duration_s}")
        if self.base_granularity_s < 1:
            raise ValueError(
                f"TfSpec.base_granularity_s must be >= 1, got {self.base_granularity_s}"
            )
        if self.duration_s % self.base_granularity_s != 0:
            raise ValueError(
                f"TfSpec.duration_s ({self.duration_s}) must be an integer multiple "
                f"of base_granularity_s ({self.base_granularity_s})"
            )

    @property
    def is_minute(self) -> bool:
        """True when the duration is a whole number of minutes."""
        return self.duration_s % _MINUTE_S == 0

    @property
    def minutes(self) -> int:
        """The duration in whole minutes; raises for a sub-minute spec (fail-loud)."""
        if not self.is_minute:
            raise ValueError(
                f"TfSpec {self.label!r} is not a whole-minute timeframe "
                f"({self.duration_s} s) — no honest minutes value exists"
            )
        return self.duration_s // _MINUTE_S


def _minute_spec(minutes: int, lake_label: str) -> TfSpec:
    """Build a whole-minute session-anchored registry entry."""
    return TfSpec(
        label=f"{minutes}m",
        duration_s=minutes * _MINUTE_S,
        base_granularity_s=_MINUTE_S,
        anchor=TfAnchor.SESSION_OPEN,
        lake_label=lake_label,
    )


#: THE timeframe registry (DEC-TF-REGISTRY-DYNAMIC-001) — the single source
#: of truth the legacy minutes-keyed vocabulary below derives from.
ALL_TFS: Final[tuple[TfSpec, ...]] = (
    _minute_spec(1, "1minute"),
    _minute_spec(2, "2minute"),
    _minute_spec(3, "3minute"),
    _minute_spec(4, "4minute"),
    _minute_spec(5, "5minute"),
    _minute_spec(6, "6minute"),
    _minute_spec(7, "7minute"),
    _minute_spec(8, "8minute"),
    _minute_spec(9, "9minute"),
    _minute_spec(10, "10minute"),
    _minute_spec(11, "11minute"),
    _minute_spec(12, "12minute"),
    _minute_spec(13, "13minute"),
    _minute_spec(14, "14minute"),
    _minute_spec(15, "15minute"),
    _minute_spec(30, "30minute"),
    _minute_spec(60, "1hour"),
    _minute_spec(120, "2hour"),
    _minute_spec(180, "3hour"),
    _minute_spec(240, "4hour"),
    TfSpec(
        label="1d",
        duration_s=DAILY_TF_MINUTES * _MINUTE_S,
        base_granularity_s=_MINUTE_S,
        anchor=TfAnchor.IST_DAY,
        lake_label="1day",
    ),
)


def validate_registry(specs: Sequence[TfSpec]) -> None:
    """Raise ``ValueError`` unless *specs* is a coherent registry.

    Coherence: non-empty; unique ``label``s; unique ``lake_label``s; unique
    minute counts among the whole-minute specs (a duplicate would silently
    collapse inside the minutes-keyed derived views).
    """
    if not specs:
        raise ValueError("timeframe registry must not be empty")
    labels = [spec.label for spec in specs]
    if len(set(labels)) != len(labels):
        dupes = sorted({name for name in labels if labels.count(name) > 1})
        raise ValueError(f"duplicate TfSpec labels in registry: {dupes}")
    lake_labels = [spec.lake_label for spec in specs]
    if len(set(lake_labels)) != len(lake_labels):
        dupes = sorted({name for name in lake_labels if lake_labels.count(name) > 1})
        raise ValueError(f"duplicate TfSpec lake_labels in registry: {dupes}")
    minute_values = [spec.minutes for spec in specs if spec.is_minute]
    if len(set(minute_values)) != len(minute_values):
        dupes_m = sorted({m for m in minute_values if minute_values.count(m) > 1})
        raise ValueError(f"duplicate whole-minute durations in registry: {dupes_m}")


def tf_minutes_view(specs: Sequence[TfSpec]) -> tuple[int, ...]:
    """Minute counts of every whole-minute spec in *specs*, registry order."""
    return tuple(spec.minutes for spec in specs if spec.is_minute)


def tf_labels_view(specs: Sequence[TfSpec]) -> dict[int, str]:
    """``minutes -> short label`` for every whole-minute spec in *specs*."""
    return {spec.minutes: spec.label for spec in specs if spec.is_minute}


def lake_interval_labels_view(specs: Sequence[TfSpec]) -> dict[int, str]:
    """``minutes -> lake partition label`` for every whole-minute spec in *specs*."""
    return {spec.minutes: spec.lake_label for spec in specs if spec.is_minute}


def all_lake_labels_view(specs: Sequence[TfSpec]) -> tuple[str, ...]:
    """Lake labels of EVERY spec — sub-minute included, were any registered.

    This (not the minutes-keyed view) is what the partition whitelist
    consumes, so a future sub-minute registration reaches the lake grammar
    without touching ``brutex.data.partition``.
    """
    return tuple(spec.lake_label for spec in specs)


def spec_for_lake_label(lake_label: str, specs: Sequence[TfSpec] = ALL_TFS) -> TfSpec:
    """Resolve a lake interval label to its registry spec (ValueError if unknown)."""
    for spec in specs:
        if spec.lake_label == lake_label:
            return spec
    raise ValueError(
        f"Unknown lake interval label: {lake_label!r}. "
        f"Must be one of {sorted(spec.lake_label for spec in specs)}."
    )


def spec_for_minutes(tf_minutes: int, specs: Sequence[TfSpec] = ALL_TFS) -> TfSpec:
    """Resolve a whole-minute count to its registry spec (ValueError if unknown)."""
    for spec in specs:
        if spec.is_minute and spec.minutes == tf_minutes:
            return spec
    raise ValueError(
        f"Unsupported timeframe: {tf_minutes} minutes. "
        f"Must be one of {sorted(spec.minutes for spec in specs if spec.is_minute)}."
    )


validate_registry(ALL_TFS)

# All 21 timeframes (in minutes) — a DERIVED VIEW over the registry.
ALL_TF_MINUTES: Final[tuple[int, ...]] = tf_minutes_view(ALL_TFS)

if ALL_TF_MINUTES != (
    INTRADAY_MINUTE_TFS + INTRADAY_MULTIMINUTE_TFS + (DAILY_TF_MINUTES,)
):  # pragma: no cover — defensive module-load assertion
    raise AssertionError("ALL_TF_MINUTES view must match the grouping constants exactly")

if len(ALL_TF_MINUTES) != EXPECTED_TF_COUNT:  # pragma: no cover — defensive module-load assertion
    raise AssertionError(
        f"Expected {EXPECTED_TF_COUNT} timeframes per spec, got {len(ALL_TF_MINUTES)}"
    )


# Canonical string labels (used as Parquet partition keys and config IDs) —
# a DERIVED VIEW over the registry, byte-identical to the pre-registry literals.
TF_LABELS: Final[dict[int, str]] = tf_labels_view(ALL_TFS)

if set(TF_LABELS.keys()) != set(
    ALL_TF_MINUTES
):  # pragma: no cover — defensive module-load assertion
    raise AssertionError("TF_LABELS keys must match ALL_TF_MINUTES exactly")


# Canonical long-form LAKE interval labels (Parquet partition directory names,
# ``brutex.data.partition.VALID_INTERVALS`` members) for every Track 1
# timeframe — a DERIVED VIEW over the registry. DEC-TF-VOCAB-21-001 kept the
# minutes <-> lake-label correspondence in ONE place; DEC-TF-REGISTRY-DYNAMIC-001
# moved that one place into ``ALL_TFS``, from which ``partition.VALID_INTERVALS``
# and ``feed.resampler.INTERVAL_SECONDS`` now also derive.
LAKE_INTERVAL_LABELS: Final[dict[int, str]] = lake_interval_labels_view(ALL_TFS)

if set(LAKE_INTERVAL_LABELS.keys()) != set(
    ALL_TF_MINUTES
):  # pragma: no cover — defensive module-load assertion
    raise AssertionError("LAKE_INTERVAL_LABELS keys must match ALL_TF_MINUTES exactly")


#: EVERY registry lake label in registry order (sub-minute included, were any
#: registered) — the partition whitelist derives from THIS view.
ALL_LAKE_INTERVAL_LABELS: Final[tuple[str, ...]] = all_lake_labels_view(ALL_TFS)


def lake_interval_label(tf_minutes: int) -> str:
    """Map minute-count to the canonical long-form LAKE interval label.

    Examples:
        >>> lake_interval_label(1)
        '1minute'
        >>> lake_interval_label(60)
        '1hour'
        >>> lake_interval_label(375)
        '1day'
    """
    if tf_minutes not in LAKE_INTERVAL_LABELS:
        raise ValueError(
            f"Unsupported timeframe: {tf_minutes} minutes. "
            f"Must be one of {sorted(LAKE_INTERVAL_LABELS.keys())}."
        )
    return LAKE_INTERVAL_LABELS[tf_minutes]


def trading_minutes_for_lake_interval(interval: str) -> int:
    """Parse a lake interval label back to its TRADING-minute count.

    The daily label maps to the 375-minute NSE session (NOT 1440 wall-clock
    minutes) — this is the ``Timeframe.minutes`` the trade-sim forced-exit
    logic means (DEC-TF-VOCAB-21-001; was the latent ``1day``=1440 bug in
    ``cmd_sweep_all``'s ``INTERVAL_SECONDS // 60`` derivation).

    Examples:
        >>> trading_minutes_for_lake_interval("1minute")
        1
        >>> trading_minutes_for_lake_interval("1day")
        375
    """
    inverse = {v: k for k, v in LAKE_INTERVAL_LABELS.items()}
    if interval not in inverse:
        raise ValueError(
            f"Unknown lake interval label: {interval!r}. Must be one of {sorted(inverse.keys())}."
        )
    return inverse[interval]


def label(tf_minutes: int) -> str:
    """Map minute-count to canonical TF label.

    Examples:
        >>> label(1)
        '1m'
        >>> label(60)
        '60m'
        >>> label(375)
        '1d'
    """
    if tf_minutes not in TF_LABELS:
        raise ValueError(
            f"Unsupported timeframe: {tf_minutes} minutes. "
            f"Must be one of {sorted(TF_LABELS.keys())}."
        )
    return TF_LABELS[tf_minutes]


def parse_label(s: str) -> int:
    """Parse a canonical TF label back to its minute count.

    Examples:
        >>> parse_label("1m")
        1
        >>> parse_label("1d")
        375
    """
    inverse = {v: k for k, v in TF_LABELS.items()}
    if s not in inverse:
        raise ValueError(
            f"Unknown timeframe label: {s!r}. Must be one of {sorted(inverse.keys())}."
        )
    return inverse[s]


def is_intraday(tf_minutes: int) -> bool:
    """True if the TF is intraday (< full session length)."""
    if tf_minutes not in TF_LABELS:
        raise ValueError(f"Unsupported timeframe: {tf_minutes}")
    return tf_minutes < DAILY_TF_MINUTES


def is_minute_multiple_of(target: int, source: int) -> bool:
    """True if `target` is an integer multiple of `source` in minutes.

    Used by the resampler to validate that 1m → 5m is allowed
    (5 % 1 == 0) but 1m → 7m is also allowed (7 % 1 == 0).
    Source minute count must divide target minute count exactly.

    The daily TF (375m) is special: it's a multiple of all intraday TFs
    that divide 375 (1, 3, 5, 15, 25, 75, 125, 375). For TFs that don't
    divide 375 cleanly (2, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 30, 60,
    120, 180, 240), daily resampling uses calendar-aware bucketing.
    """
    if source <= 0 or target <= 0:
        raise ValueError("Timeframes must be positive minute counts")
    return target % source == 0
