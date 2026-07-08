"""Read-time two-side ratio primitives (DEC-MOVERS-OPTIONS-PCR-001).

ONE side-agnostic, metric-agnostic fold (CLAUDE.md §2 Golden Rule #11 COMMON)
shared by the OI-PCR and volume-PCR variants — and by any future two-side
ratio (a call/put orientation is ``fold_side_ratio(...,
numerator_side=CALL_SIDE, denominator_side=PUT_SIDE)``; a turnover-PCR is
``metric=`` a paisa accessor; a ``/db`` per-expiry PCR calls
:func:`compute_pcr` over legs built from that view's frame) without
re-architecting (DYNAMIC). Ratios are STATISTICAL metrics: full-precision
:class:`decimal.Decimal`, NEVER ``round_money``'d (DEC-MONEY-2DP-001).
Nothing is ever fabricated: a zero denominator is a TYPED sentinel (never
``Infinity``), a missing metric cell is excluded + counted, and a corrupt
negative cell / unknown side / coverage wiring mismatch raises LOUDLY
(Golden Rules #1/#10; CLAUDE.md §4 narrow-catch philosophy). READ-TIME ONLY
— never stored, never in the 60-condition vocabulary, never in run identity.

Stdlib-only and cycle-proof: imports NOTHING from ``brutex.dashboard.*`` /
``brutex.data.*`` (the :mod:`brutex.data.moneyness` precedent — a shared
read-time derivation any surface can call).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import Enum
from typing import Final

__all__ = [
    "CALL_SIDE",
    "COVERAGE_EMPTY",
    "COVERAGE_FULL",
    "COVERAGE_PARTIAL",
    "PUT_SIDE",
    "PcrCorruptionError",
    "PcrCoverage",
    "PcrResult",
    "RatioOutcome",
    "RatioStatus",
    "compute_pcr",
    "fold_side_ratio",
]


class PcrCorruptionError(ValueError):
    """A negative metric cell reached the fold — lake VALUE corruption.

    OI/volume are non-negative by lake contract (the ingest boundary clamps
    ``max(0, …)`` and :class:`~brutex.data.models.CandleBar` rejects
    negatives), so a negative cell can only come from an OUT-OF-BAND write —
    a value-corrupted Parquet file. Subclasses :class:`ValueError` so the
    fold's fail-loud law (§1.1 clause 3) is byte-unchanged for every existing
    caller, while a ROUTE can distinguish server-side lake corruption from a
    client-input ``ValueError``: the ``/api/v1/movers`` route answers an
    honest 500 for this error, never its client-error 400 mapping
    (DEC-MOVERS-OPTIONS-PCR-001 round-1 hostile-review fold).
    """


#: Canonical option side tokens (the vendor CE/PE vocabulary, CLAUDE.md §6).
PUT_SIDE: Final[str] = "PE"
CALL_SIDE: Final[str] = "CE"

#: Coverage label — every tier leg read AND every metric cell present.
COVERAGE_FULL: Final[str] = "full"
#: Coverage label — ≥1 leg unread OR ≥1 read leg missing a metric cell.
COVERAGE_PARTIAL: Final[str] = "partial"
#: Coverage label — the chain exists (or is vacuous) but ZERO legs were readable.
COVERAGE_EMPTY: Final[str] = "empty"

#: The EXPLICITLY PINNED ratio-division context — the default context's values
#: (28 significant digits, banker's rounding), pinned so the wire string can
#: never be skewed by an ambient thread-local ``decimal`` context mutation
#: (the NFR-DET-001 precedent: ``execution/costs.py`` pins ``ROUND_HALF_EVEN``
#: explicitly because relying on the process-global context is a cross-module
#: non-determinism vector). ``localcontext`` COPIES this, so the constant is
#: never mutated.
_RATIO_CONTEXT: Final[Context] = Context(prec=28, rounding=ROUND_HALF_EVEN)


class RatioStatus(Enum):
    """The SINGLE zero-denominator sentinel type (shared by every ratio variant).

    ``OK`` — the denominator sum was positive and the ratio was computed.
    ``ZERO_DENOMINATOR`` — the denominator sum was zero (including the 0/0
    and empty-input cases): the ratio is ``None``, NEVER ``Infinity`` and
    never a fabricated number (Golden Rule #1).
    """

    OK = "ok"
    ZERO_DENOMINATOR = "zero_denominator"


@dataclass(frozen=True, slots=True)
class RatioOutcome:
    """Side-agnostic result of ONE :func:`fold_side_ratio` pass over one metric.

    Invariant (property-tested): ``status is RatioStatus.OK`` ⟺ ``ratio is
    not None``. The raw sums always survive so "puts alive, calls dead" is
    visible even when the ratio itself is undefined.

    Attributes
    ----------
    ratio
        ``numerator_sum / denominator_sum`` at FULL ``Decimal`` precision
        (a statistical metric — never money-rounded, DEC-MONEY-2DP-001), or
        ``None`` when the denominator sum is zero.
    status
        :attr:`RatioStatus.OK` or :attr:`RatioStatus.ZERO_DENOMINATOR`.
    numerator_sum
        Σ metric over numerator-side legs whose cell was present.
    denominator_sum
        Σ metric over denominator-side legs whose cell was present.
    missing_legs
        Count of legs whose metric cell was ``None`` — excluded from BOTH
        sums, never treated as ``0`` (Golden Rule #1).
    """

    ratio: Decimal | None
    status: RatioStatus
    numerator_sum: int
    denominator_sum: int
    missing_legs: int


@dataclass(frozen=True, slots=True)
class PcrCoverage:
    """How much of the discovered chain the sums actually saw.

    FAIL-LOUD construction: ``__post_init__`` raises :class:`ValueError` if
    either count is negative or ``legs_read > legs_total`` — a denominator
    wiring bug must trip, never read as fake-full coverage (Golden Rule #10).

    Attributes
    ----------
    legs_read
        ``n`` — legs that produced a session read.
    legs_total
        ``m`` — the tier-filtered chain size, counted BEFORE any read;
        always ``>= legs_read``.
    """

    legs_read: int
    legs_total: int

    def __post_init__(self) -> None:
        """Validate the coverage counts (fail-loud — never fake-full).

        Raises
        ------
        ValueError
            When either count is negative, or ``legs_read > legs_total``.
        """
        if self.legs_read < 0 or self.legs_total < 0:
            raise ValueError(
                f"negative coverage count: legs_read={self.legs_read!r}, "
                f"legs_total={self.legs_total!r}"
            )
        if self.legs_read > self.legs_total:
            raise ValueError(
                f"legs_read ({self.legs_read!r}) exceeds legs_total ({self.legs_total!r}) — "
                "a coverage wiring mismatch, never fake-full coverage"
            )

    @property
    def is_partial(self) -> bool:
        """``True`` when at least one discovered leg produced no read."""
        return self.legs_read < self.legs_total


@dataclass(frozen=True, slots=True)
class PcrResult:
    """Both PCR variants + coverage for one (underlying, tier-expiry) chain.

    Attributes
    ----------
    oi
        The OI fold — numerator = ``PE``, denominator = ``CE``.
    volume
        The volume fold — same orientation; independent of :attr:`oi`.
    coverage
        The ``n``-of-``m`` chain coverage the sums were computed over.
    """

    oi: RatioOutcome
    volume: RatioOutcome
    coverage: PcrCoverage

    @property
    def oi_pcr(self) -> Decimal | None:
        """The OI put-call ratio (``None`` on a zero call-OI denominator)."""
        return self.oi.ratio

    @property
    def vol_pcr(self) -> Decimal | None:
        """The volume put-call ratio (``None`` on a zero call-volume denominator)."""
        return self.volume.ratio

    @property
    def put_oi(self) -> int:
        """Σ open interest over the put legs (the OI fold's numerator sum)."""
        return self.oi.numerator_sum

    @property
    def call_oi(self) -> int:
        """Σ open interest over the call legs (the OI fold's denominator sum)."""
        return self.oi.denominator_sum

    @property
    def put_volume(self) -> int:
        """Σ session volume over the put legs (the volume fold's numerator sum)."""
        return self.volume.numerator_sum

    @property
    def call_volume(self) -> int:
        """Σ session volume over the call legs (the volume fold's denominator sum)."""
        return self.volume.denominator_sum

    @property
    def coverage_label(self) -> str:
        """The honesty vocabulary of this row.

        :data:`COVERAGE_EMPTY` when ``legs_read == 0`` (including the vacuous
        ``legs_total == 0`` chain); :data:`COVERAGE_FULL` iff every discovered
        leg was read AND both metric folds saw every cell; else
        :data:`COVERAGE_PARTIAL`.
        """
        if self.coverage.legs_read == 0:
            return COVERAGE_EMPTY
        if (
            not self.coverage.is_partial
            and self.oi.missing_legs == 0
            and self.volume.missing_legs == 0
        ):
            return COVERAGE_FULL
        return COVERAGE_PARTIAL

    def coverage_note(self) -> str | None:
        """The ONE honest coverage wording every surface shows (``None`` = full).

        Deterministic fixed-order composition joined by ``" · "``:
        ``"{n}/{m} legs read"`` when :attr:`PcrCoverage.is_partial`;
        ``"{k} leg(s) missing OI"`` when ``oi.missing_legs > 0``;
        ``"{k} leg(s) missing volume"`` when ``volume.missing_legs > 0``.
        A vacuous chain (``legs_total == 0``) reads ``"no legs"``. Never
        re-derived by a frontend — one wording source (Golden Rule #11).

        Returns
        -------
        str | None
            The composed caveat, or ``None`` when coverage is full.
        """
        if self.coverage.legs_total == 0:
            return "no legs"
        parts: list[str] = []
        if self.coverage.is_partial:
            parts.append(f"{self.coverage.legs_read}/{self.coverage.legs_total} legs read")
        if self.oi.missing_legs > 0:
            parts.append(f"{self.oi.missing_legs} leg(s) missing OI")
        if self.volume.missing_legs > 0:
            parts.append(f"{self.volume.missing_legs} leg(s) missing volume")
        if not parts:
            return None
        return " · ".join(parts)


def fold_side_ratio[LegT](
    legs: Iterable[LegT],
    *,
    side_of: Callable[[LegT], str],
    metric: Callable[[LegT], int | None],
    numerator_side: str,
    denominator_side: str,
) -> RatioOutcome:
    """The ONE two-side ratio fold — a single O(legs) pass, duck-typed input.

    Any leg object folds via the two callables (no adapter padding, no base
    class). The exact law, each clause pinned by a test:

    1. A leg whose ``side_of`` is neither *numerator_side* nor
       *denominator_side* raises :class:`ValueError` — a wrong side reaching
       the fold is a programming error, never a silent third bucket.
    2. ``metric(leg) is None`` → excluded from BOTH sums,
       ``missing_legs += 1`` — an absent cell is NEVER treated as ``0``.
    3. ``metric(leg) < 0`` → :class:`PcrCorruptionError` (a :class:`ValueError`
       subclass) — OI/volume are non-negative by lake contract; a negative
       cell is corruption and must trip. The distinct type lets a route map
       lake corruption to an honest 500, never a client-error 400.
    4. ``denominator_sum > 0`` → ``ratio = Decimal(numerator_sum) /
       Decimal(denominator_sum)`` under the EXPLICITLY PINNED
       :data:`_RATIO_CONTEXT` (28 significant digits, ``ROUND_HALF_EVEN`` —
       the default context's values, pinned via ``localcontext`` so the
       determinism is STRUCTURAL, never a bet on the ambient thread-local
       context; full precision, never ``round_money``).
    5. ``denominator_sum == 0`` (including 0/0 and the empty iterable) →
       ``ratio = None``, :attr:`RatioStatus.ZERO_DENOMINATOR` — never
       ``Infinity``; the raw sums stay in the outcome.

    Parameters
    ----------
    legs
        The leg objects to fold (any type — duck-typed via the callables).
    side_of
        Maps a leg to its side token (e.g. ``"CE"`` / ``"PE"``).
    metric
        Maps a leg to its metric cell (a non-negative int), or ``None`` for
        an honestly-missing cell.
    numerator_side, denominator_side
        The two side tokens; must differ (fail-loud wiring).

    Returns
    -------
    RatioOutcome
        The fold result (``status is OK`` ⟺ ``ratio is not None``).

    Raises
    ------
    PcrCorruptionError
        On a negative metric cell (lake VALUE corruption; a
        :class:`ValueError` subclass).
    ValueError
        On identical side tokens or an unknown leg side (caller wiring bugs).
    """
    if numerator_side == denominator_side:
        raise ValueError(f"numerator and denominator sides are identical: {numerator_side!r}")
    numerator_sum = 0
    denominator_sum = 0
    missing_legs = 0
    for leg in legs:
        side = side_of(leg)
        if side not in (numerator_side, denominator_side):
            raise ValueError(f"unknown side {side!r}")
        value = metric(leg)
        if value is None:
            missing_legs += 1
            continue
        if value < 0:
            raise PcrCorruptionError(f"negative metric {value!r}")
        if side == numerator_side:
            numerator_sum += value
        else:
            denominator_sum += value
    if denominator_sum > 0:
        with localcontext(_RATIO_CONTEXT):
            ratio = Decimal(numerator_sum) / Decimal(denominator_sum)
        return RatioOutcome(
            ratio=ratio,
            status=RatioStatus.OK,
            numerator_sum=numerator_sum,
            denominator_sum=denominator_sum,
            missing_legs=missing_legs,
        )
    return RatioOutcome(
        ratio=None,
        status=RatioStatus.ZERO_DENOMINATOR,
        numerator_sum=numerator_sum,
        denominator_sum=denominator_sum,
        missing_legs=missing_legs,
    )


def compute_pcr[LegT](
    legs: Sequence[LegT],
    *,
    side_of: Callable[[LegT], str],
    oi_of: Callable[[LegT], int | None],
    volume_of: Callable[[LegT], int | None],
    legs_total: int,
) -> PcrResult:
    """Both PCR variants via TWO calls to the one fold (zero duplication) + coverage.

    Parameters
    ----------
    legs
        The successfully-read legs of one (underlying, tier-expiry) chain.
    side_of
        Maps a leg to its ``"CE"`` / ``"PE"`` side token.
    oi_of
        Maps a leg to its as-of open interest, or ``None`` when missing.
    volume_of
        Maps a leg to its session volume, or ``None`` when missing.
    legs_total
        The pre-read tier chain size (``m``); ``legs_total < len(legs)``
        raises :class:`ValueError` via :class:`PcrCoverage` (fail-loud
        wiring — never fake-full coverage).

    Returns
    -------
    PcrResult
        The OI + volume outcomes (put = numerator by construction) and the
        ``n``-of-``m`` coverage.

    Raises
    ------
    PcrCorruptionError
        On a negative metric cell (lake VALUE corruption; a
        :class:`ValueError` subclass).
    ValueError
        On any other fold-law violation (unknown side / identical sides) or
        a coverage wiring mismatch.
    """
    oi = fold_side_ratio(
        legs,
        side_of=side_of,
        metric=oi_of,
        numerator_side=PUT_SIDE,
        denominator_side=CALL_SIDE,
    )
    volume = fold_side_ratio(
        legs,
        side_of=side_of,
        metric=volume_of,
        numerator_side=PUT_SIDE,
        denominator_side=CALL_SIDE,
    )
    return PcrResult(
        oi=oi,
        volume=volume,
        coverage=PcrCoverage(legs_read=len(legs), legs_total=legs_total),
    )
