"""Derived-timeframe **materialiser** — the testable core (DEC-TF-MATERIALIZE-001).

The brute-force sweep evaluates strategies at all 21 timeframes (CLAUDE.md §1).
Only the **1-minute** base is fetched from the vendor; the 19 derived intraday
timeframes are *resampled* from it (DEC-DHAN-1M-ONLY-001) by
:func:`~brutex.data.spot_resample.derive_intraday_tfs`. Re-resampling 1m on
every read would re-pay that cost ~33 billion times across a production sweep.
This module **materialises** a selected derived timeframe **once** — writing it
as its own stored interval partition — so the sweep then reads a 5m / 15m / 60m
bar in **O(1)** (a direct Parquet read), CLAUDE.md §5.

## The ONE automatic prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001)

The manual ``POST /api/v1/universe/derive-timeframes`` route
(DEC-TF-MATERIALIZE-001) and the ``BRUTEX_AUTO_DERIVE_TFS`` pull-settle
auto-derive (DEC-TF-MATERIALIZE-002) are both RETIRED: derived timeframes are
now written ONLY by the pipeline's automatic FINAL prebuild phase
(:mod:`brutex.dashboard.pipeline_prebuild`), which runs strictly AFTER a
fully-successful 1-minute fetch and covers SPOT + FUTURES + **OPTIONS**, each
with the 19 intraday timeframes AND the derived ``1day`` partition
(``include_daily=True`` on :func:`materialize_lake_timeframes`). Never mid-pull.

## Pure core, injected I/O (CLAUDE.md §11 COMMON / §4 testability)

:func:`materialize_timeframes` is pure orchestration: the 1-minute **read** and
the per-timeframe **write** are *injected* callables, so the whole control flow
(de-dup, derive, route-each-frame, skip-unknown) is 100% unit-testable with
fakes — no lake, no DuckDB, no FastAPI. The raw lake I/O lives in the route
glue (``# pragma: no cover`` — integration-tested, not line-gated). The actual
resampling is **reused** wholesale from
:func:`~brutex.data.spot_resample.derive_intraday_tfs` (the single source of
truth for the session/DST/no-look-ahead bucketing) — this module never
re-implements it.

## Storable timeframes

The resampler emits all **19** derived intraday timeframes, and since
DEC-TF-VOCAB-21-001 the lake partition layer
(``brutex.data.partition.VALID_INTERVALS``) addresses every one of them.
:data:`INTRADAY_TF_INTERVALS` is the canonical TF-label → partition interval
map (all 19, derived from ``brutex.data.timeframes``); a requested label
absent from a derivation — or absent from the derived dict — is reported as
``skipped``, never written.
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass
from enum import Enum
from itertools import chain
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.derive_lock import derive_write_lock

# Re-export: `frame_to_candle_bars` LIVES in `spot_daily_resample` — the
# import-graph LEAF of the pair (this module already imports its daily-derive
# seam) — so the shared batched converter (DEC-PREBUILD-GIL-VECTORIZE-001) is
# one-directional and the former `spot_daily_resample ↔ timeframe_materialize`
# cycle (CodeQL py/cyclic-import) cannot re-form. Kept in `__all__` for the
# original call sites/tests.
from brutex.data.spot_daily_resample import frame_to_candle_bars
from brutex.data.timeframes import (
    ALL_TF_MINUTES,
    DAILY_TF_MINUTES,
    LAKE_INTERVAL_LABELS,
    TF_LABELS,
)
from brutex.observability.progress_beat import emit_progress_beat

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from datetime import date, datetime
    from pathlib import Path

    import polars as pl

    from brutex.data.models import CandleBar
    from brutex.models import Instrument

_LOG = structlog.get_logger(__name__)

#: Canonical TF-label → lake partition interval map for ALL 19 derived
#: intraday timeframes (DEC-TF-VOCAB-21-001 — derived from the canonical
#: ``brutex.data.timeframes`` maps, CLAUDE.md §11 COMMON). Keys are the
#: :data:`~brutex.data.timeframes.TF_LABELS` the resampler emits; values are
#: the ``brutex.data.partition.VALID_INTERVALS`` member each materialises
#: into. Pre-DEC-TF-VOCAB-21-001 only 8 of the 19 were storable (the lake
#: lacked labels for ``4m, 6m … 14m, 120m, 180m``); the full ladder is now
#: addressable, so every derived TF is storable. An unknown / non-derived
#: requested label is still reported as ``skipped``, never written.
INTRADAY_TF_INTERVALS: Final[dict[str, str]] = {
    TF_LABELS[minutes]: LAKE_INTERVAL_LABELS[minutes]
    for minutes in ALL_TF_MINUTES
    if minutes not in (1, DAILY_TF_MINUTES)  # 1m is the fetched base; 1d is fetched natively
}

#: The materialisable timeframe labels, in canonical (ascending-minutes) order —
#: the default selection when the caller omits ``tfs``.
MATERIALIZABLE_TFS: Final[tuple[str, ...]] = tuple(INTRADAY_TF_INTERVALS)

#: The lake interval label of the DERIVED daily partition — the key
#: ``materialize_lake_timeframes(include_daily=True)`` reports its daily bar
#: count under in :attr:`MaterializeResult.written` (DEC-PIPELINE-AUTOPREBUILD-001).
DAILY_TF_LABEL: Final[str] = "1day"


def _now_ist() -> datetime:
    """The current IST wall-clock instant — the ONE injectable clock seam for
    the default daily-derive cutoff (tests freeze THIS, never the cutoff law —
    DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001). Delegates to the COMMON
    :func:`brutex.calendars.nse.now_ist` clock leaf
    (DEC-DAILY-CUTOFF-GROUNDED-001) — the name survives as the monkeypatch
    seam."""
    from brutex.calendars.nse import now_ist

    return now_ist()


#: Env kill switch for the data-grounded daily cutoff
#: (DEC-DAILY-CUTOFF-GROUNDED-001). Default ON; ``0`` / ``off`` / ``false`` /
#: ``no`` (case-insensitive) reverts to the pure clock arm of
#: DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 at runtime (the graceful revert).
DAILY_CUTOFF_GROUNDED_ENV: Final[str] = "BRUTEX_DAILY_CUTOFF_GROUNDED"


def daily_cutoff_grounding_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``True`` unless :data:`DAILY_CUTOFF_GROUNDED_ENV` is an explicit off-value.

    The escape hatch (the exact ``BRUTEX_PREBUILD_INCREMENTAL`` idiom — ONE
    shared off-value set, :data:`brutex.data.derive_stamp.DISABLED_ENV_VALUES`):
    unset / empty / any other value ⇒ grounding is ON for dense (index)
    symbols; an off-value ⇒ every run resolves the pure clock cutoff.
    """
    import os

    from brutex.data.derive_stamp import DISABLED_ENV_VALUES

    source = env if env is not None else os.environ
    return source.get(DAILY_CUTOFF_GROUNDED_ENV, "").strip().lower() not in DISABLED_ENV_VALUES


def month_partition_files(base: Path, months: Iterable[tuple[int, int]]) -> list[str]:
    """The EXISTING ``<base>/<yyyy>/<mm>.parquet`` files for explicit *months* — pure.

    DEC-TF-REBUILD-MONTH-BOUND-001: the month-bounded read core for
    :func:`make_lake_read_1m` (the explicit-month twin of
    :func:`~brutex.data.spot_daily_resample.ist_span_partition_files`, which is
    span-derived — this one takes the caller's exact ``(year, month)`` set, e.g.
    the lake healer's touched-months set). Only months that EXIST on disk are
    returned (an absent month is skipped cleanly), as ascending POSIX path
    strings ready for :func:`polars.scan_parquet`. De-duplicated + sorted, so a
    repeated month is read once and the order is deterministic (Golden Rule #7).

    CORRECTNESS BASIS (the invariant this bounding rests on, pinned by
    ``tests/unit/test_calendar_nse_utc_month_invariant.py``): every NSE trading
    session — regular, special-weekend Budget Saturday/Sunday, and the Diwali
    Muhurat evening — opens AND closes on the SAME UTC calendar date as its IST
    session date (09:15–15:30 IST = 03:45–10:00 UTC; even a Muhurat close at
    ~19:15 IST is 13:45 UTC), so an intraday session NEVER straddles a UTC
    month boundary and a month's derived intraday-TF bars are fully computable
    from that month's own 1-minute partition. The derived ``1day`` does NOT
    share the invariant (midnight-IST stamping lands an IST date-D bar in the
    UTC month of D−1), which is why ``months`` + ``include_daily`` is refused
    in :func:`materialize_lake_timeframes`.
    """
    files: list[str] = []
    for year, month in sorted(set(months)):
        candidate = base / f"{year:04d}" / f"{month:02d}.parquet"
        if candidate.is_file():
            files.append(candidate.as_posix())
    return files


class PrebuildKind(Enum):
    """How an instrument's derived timeframes are pre-built (DEC-PREBUILD-SELECT-001).

    The pre-build is SEGMENT-AWARE — the lake stores OI for F&O but never for
    spot, so the two surfaces use a DIFFERENT resampler.

    Members
    -------
    SPOT
        An ``INDEX`` / ``CASH`` spot row (the 3 brute-force indices, India VIX,
        and the F&O-eligible / NTM single-stock SPOT rows). Resampled by the
        OI-dropping :func:`brutex.data.spot_resample.derive_intraday_tfs`.
    FUTURES
        An ``FNO`` ``FUT`` contract. Resampled by the OI-aware
        :func:`brutex.data.fno_resample.derive_fno_intraday_tfs` so the
        ``open_interest`` snapshot is preserved per bar.
    OPTIONS
        An ``FNO`` ``CE`` / ``PE`` contract. Materialised OI-aware exactly like
        FUTURES (option bars are OI-carrying F&O bars) since
        DEC-PIPELINE-AUTOPREBUILD-001, which SUPERSEDES the earlier on-demand
        design (DEC-PREBUILD-SELECT-001's explicit no-op). Honest storage
        envelope (Golden Rule #10, DEC-LAKE-FOOTPRINT-MEASURED-001): the
        fully-materialised cost is a MEASURED multiple of the 1-minute
        footprint — the observed multiplier is persisted to ``schema_meta``
        (``lake_footprint.*``) after every prebuild run, with a conservative
        **6.5×** fallback before the first observation. The retired "~2-3×"
        estimate IGNORED the 17-column Greeks-enriched option 1-minute rewrite
        (:mod:`brutex.data.greeks_enrich`), which alone pushes the realistic
        all-in cost toward ~5-6× — operator-accepted either way.
    """

    SPOT = "spot"
    FUTURES = "futures"
    OPTIONS = "options"


def prebuild_kind_for(instrument: Instrument) -> PrebuildKind:
    """Classify an instrument's intraday pre-build kind (DEC-PREBUILD-SELECT-001).

    Pure (no I/O) — derives the kind from the instrument's
    :class:`~brutex.models.Segment` + :class:`~brutex.models.InstrumentType`:

    * ``FNO`` ``CE`` / ``PE`` → :attr:`PrebuildKind.OPTIONS` (OI-aware, like
      FUTURES — DEC-PIPELINE-AUTOPREBUILD-001).
    * ``FNO`` ``FUT`` (or any other F&O type) → :attr:`PrebuildKind.FUTURES`
      (OI-aware). The non-CE/PE F&O fallback is FUTURES so a forward-compat F&O
      type can never silently route through the OI-dropping SPOT resampler.
    * everything else (``INDEX`` / ``CASH`` spot) → :attr:`PrebuildKind.SPOT`.
    """
    from brutex.models import InstrumentType, Segment

    if instrument.segment is Segment.FNO:
        if instrument.instrument in (InstrumentType.CE, InstrumentType.PE):
            return PrebuildKind.OPTIONS
        return PrebuildKind.FUTURES
    return PrebuildKind.SPOT


def intraday_resampler_for(
    kind: PrebuildKind,
) -> Callable[[pl.DataFrame], dict[str, pl.DataFrame]]:
    """Return the intraday resampler for a pre-build *kind*.

    The single COMMON routing point (CLAUDE.md §11) from a
    :class:`PrebuildKind` to the canonical resampler:

    * :attr:`PrebuildKind.SPOT` → :func:`brutex.data.spot_resample.derive_intraday_tfs`
      (byte-identical to the pre-DEC-PREBUILD-SELECT-001 default — spot output
      is unchanged).
    * :attr:`PrebuildKind.FUTURES` **and** :attr:`PrebuildKind.OPTIONS` →
      :func:`brutex.data.fno_resample.derive_fno_intraday_tfs` (OI-aware —
      preserves ``open_interest``; option bars are OI-carrying F&O bars, same
      as futures). OPTIONS returned ``None`` (on-demand only) before
      DEC-PIPELINE-AUTOPREBUILD-001 enabled option materialisation.
    """
    if kind is PrebuildKind.SPOT:
        from brutex.data.spot_resample import derive_intraday_tfs

        return derive_intraday_tfs
    from brutex.data.fno_resample import derive_fno_intraday_tfs

    return derive_fno_intraday_tfs


def daily_resample_fn_for(
    kind: PrebuildKind,
) -> Callable[[pl.DataFrame, date], list[CandleBar]]:
    """Return the positional 1-minute → ``1day`` resampler for a pre-build *kind*.

    The single COMMON routing point (CLAUDE.md §11) from a :class:`PrebuildKind`
    to the canonical daily derivation — EXTRACTED from the (now removed)
    mid-pull ``historical_pull._derive_daily_from_minute`` OI-selection so the
    prebuild phase is the ONE place the choice lives
    (DEC-PIPELINE-AUTOPREBUILD-001):

    * :attr:`PrebuildKind.SPOT` →
      :func:`brutex.data.spot_daily_resample.resample_1m_to_daily_default`
      (spot/index daily bars carry ``open_interest=None``).
    * :attr:`PrebuildKind.FUTURES` / :attr:`PrebuildKind.OPTIONS` →
      :func:`brutex.data.fno_daily_resample.resample_1m_to_fno_daily_positional`
      (carry-last OI — DEC-FNO-TF-DERIVE-OI-001).
    """
    if kind is PrebuildKind.SPOT:
        from brutex.data.spot_daily_resample import resample_1m_to_daily_default

        return resample_1m_to_daily_default
    from brutex.data.fno_daily_resample import resample_1m_to_fno_daily_positional

    return resample_1m_to_fno_daily_positional


@dataclass(frozen=True, slots=True)
class MaterializeResult:
    """Outcome of one :func:`materialize_timeframes` call.

    Attributes
    ----------
    written
        ``{tf_label: bar_count}`` for every requested timeframe that was
        present in the derivation and written, in request (de-duplicated)
        order. ``bar_count`` is whatever the injected ``write_tf`` returned.
    skipped
        The requested timeframe labels that were NOT written because the
        derivation produced no frame for them (an unknown / non-derived /
        non-storable label), in request (de-duplicated) order.
    unchanged
        ``True`` iff the whole derive was SKIPPED because the symbol's
        1-minute source was byte-for-byte unchanged since its last stamped
        derive (DEC-PREBUILD-INCREMENTAL-001 — ``written`` is then empty and
        the lake state is provably identical to a full re-derive, which the
        latest-wins derived writers (DEC-TF-REMAT-LATEST-WINS-001) would have
        rewritten byte-identically anyway). ``False``
        for every run that actually read + resampled.
    opens_substituted
        SPOT-only OBSERVE count (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001): how
        many COMPLETE sessions in this symbol's 1-minute frame had their
        derived daily open silently substituted (09:15 bar absent — the
        implicit ``open.first()`` fallback). ``0`` for F&O kinds (never
        scanned — spot-only by decision), for ``include_daily=False`` runs
        (no daily bar derived, nothing to observe), and for an
        ``unchanged=True`` incremental skip (nothing was re-read). Purely
        observational — the written bars are byte-identical either way.
    """

    written: dict[str, int]
    skipped: tuple[str, ...]
    unchanged: bool = False
    opens_substituted: int = 0

    @property
    def total_bars(self) -> int:
        """Total bars written across every materialised timeframe."""
        return sum(self.written.values())


def materialize_timeframes(
    *,
    tfs: Iterable[str],
    read_1m: Callable[[], pl.DataFrame],
    write_tf: Callable[[str, pl.DataFrame], int],
    resampler: Callable[[pl.DataFrame], dict[str, pl.DataFrame]] | None = None,
) -> MaterializeResult:
    """Resample stored 1-minute bars → write each selected derived timeframe.

    Reads the symbol's 1-minute bars **once** (via *read_1m*), resamples them
    to the 19 derived intraday timeframes via the injected *resampler* (default
    :func:`~brutex.data.spot_resample.derive_intraday_tfs`, the SPOT path —
    byte-identical to the pre-DEC-PREBUILD-SELECT-001 behaviour), then writes
    each **requested** timeframe present in that derivation via *write_tf*. A
    requested label with no derived frame (unknown / non-storable) is recorded
    in :attr:`MaterializeResult.skipped`, never written. Pure orchestration —
    all lake / disk I/O is injected, so this is 100% unit-testable.

    Parameters
    ----------
    tfs
        The requested timeframe labels (e.g. ``("5m", "15m", "60m")``).
        De-duplicated preserving first-seen order via ``dict.fromkeys``
        (O(1)-per-item uniqueness) so a label is never derived or written
        twice — re-listing ``5m`` twice writes it once.
    read_1m
        Zero-arg callable returning the symbol's stored 1-minute bars as a
        sorted :class:`polars.DataFrame` (columns ``ts_utc, open, high, low,
        close, volume``). Injected so the core never touches the lake.
    write_tf
        ``(tf_label, frame) -> bar_count`` callable that persists one
        resampled frame to its own stored interval partition and returns the
        number of bars written. Injected so the core never touches the sink.
    resampler
        The ``df_1m -> {tf_label: frame}`` intraday derivation (SEGMENT-AWARE,
        DEC-PREBUILD-SELECT-001). ``None`` (the default) selects the SPOT
        resampler so an un-parametrised call is byte-for-byte unchanged; an
        F&O futures/options caller passes the OI-aware
        :func:`brutex.data.fno_resample.derive_fno_intraday_tfs` (typically via
        :func:`intraday_resampler_for` — options materialise like futures per
        DEC-PIPELINE-AUTOPREBUILD-001).

    Returns
    -------
    MaterializeResult
        The per-timeframe written bar counts + the skipped labels.
    """
    if resampler is None:
        # Local import keeps the module's import graph free of polars for callers
        # that only need the dataclass / the interval map (CLAUDE.md §11 lazy dep,
        # mirroring spot_resample's own polars-only-when-used discipline).
        from brutex.data.spot_resample import derive_intraday_tfs

        resampler = derive_intraday_tfs

    # De-dup preserving first-seen order — O(1) uniqueness, deterministic order
    # (CLAUDE.md §5 / §7). A duplicate label derives + writes exactly once.
    requested = tuple(dict.fromkeys(tfs))

    derived = resampler(read_1m())

    written: dict[str, int] = {}
    skipped: list[str] = []
    for tf in requested:
        # Liveness beat per derived timeframe (DEC-PIPELINE-STALL-HEARTBEAT-001):
        # a deep-history symbol's 20-timeframe materialise can run minutes — beat
        # WITHIN the symbol so the pull stall watchdog never false-declares a
        # healthy prebuild HUNG. No-op outside a dashboard pull; O(1).
        emit_progress_beat()
        frame = derived.get(tf)
        if frame is None:
            skipped.append(tf)
            continue
        written[tf] = write_tf(tf, frame)

    return MaterializeResult(written=written, skipped=tuple(skipped))


def _derived_dirs_present(
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    tfs: tuple[str, ...],
    include_daily: bool,
) -> bool:
    """``True`` iff every requested derived interval dir exists for the symbol.

    The cheap O(#TFs ≤ 20) existence guard behind the incremental skip
    (DEC-PREBUILD-INCREMENTAL-001): a stamp can only skip when the derived
    tree it vouches for is actually on disk — an out-of-band deletion (or a
    heal rebuild that failed after its quarantine-move) falls through to a
    full re-derive instead of silently leaving the partitions missing.
    Dir-granular (honest envelope: individual month FILES inside a present
    interval dir are not re-verified — the token + heal stamp-removal cover
    the paths that mutate them).
    """
    symbol_dir = lake_root / "bars" / exchange / segment / symbol
    for tf in tfs:
        interval = INTRADAY_TF_INTERVALS.get(tf)
        # A label with no storable interval is reported `skipped` by a full
        # run too, so it needs no partition to exist.
        if interval is not None and not (symbol_dir / interval).is_dir():
            return False
    return not include_daily or (symbol_dir / DAILY_TF_LABEL).is_dir()


def materialize_lake_timeframes(
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    kind: PrebuildKind,
    tfs: Iterable[str] = MATERIALIZABLE_TFS,
    include_daily: bool = False,
    daily_before: date | None = None,
    daily_cutoff_now: datetime | None = None,
    months: Iterable[tuple[int, int]] | None = None,
    incremental: bool | None = None,
    dense_symbol: bool = False,
) -> MaterializeResult:
    """SEGMENT-AWARE materialise of ONE lake partition's derived TFs (DEC-PREBUILD-SELECT-001).

    The ONE shared primitive (CLAUDE.md §11 COMMON) behind the pipeline's
    automatic final prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001 — the manual
    route + the pull-settle auto-derive are retired). Given a lake partition's
    coordinates + its :class:`PrebuildKind`, it:

    * picks the resampler via :func:`intraday_resampler_for`
      (SPOT → OI-dropping spot resampler; FUTURES/OPTIONS → OI-aware F&O
      resampler — option materialisation enabled by
      DEC-PIPELINE-AUTOPREBUILD-001, which supersedes the on-demand-only
      design; honest cost per DEC-LAKE-FOOTPRINT-MEASURED-001: a MEASURED
      multiple of the 1-minute footprint — observed multiplier persisted to
      ``schema_meta``, conservative 6.5× fallback; the retired "~2-3×" guess
      ignored the Greeks-enriched 17-column 1-minute rewrite — operator-
      accepted);
    * reads the 1-minute base ONCE, with the OI column for FUTURES + OPTIONS
      (``with_open_interest=kind is not PrebuildKind.SPOT``), so a spot
      partition's output is byte-identical to before;
    * writes each derived intraday TF (the writer carries OI when the frame
      has it);
    * with ``include_daily=True``, ALSO derives the symbol's ``1day`` partition
      from the SAME 1-minute frame — the spot midnight-IST daily for SPOT, the
      carry-last-OI F&O daily for FUTURES/OPTIONS
      (:func:`daily_resample_fn_for`) — written through the SHARED
      :func:`~brutex.data.spot_daily_resample.make_lake_write_1day` writer
      (latest-wins on the derived ``1day``, 2-dp tick snap at the sink). The
      daily bar count joins the result under :data:`DAILY_TF_LABEL`.

    ``daily_before`` is the forming-session cutoff (an IST date); an EXPLICIT
    date is absolute LAW (no grounding — the lake-heal months rebuild, the
    doctor's ``probe_day + 1`` call, and every explicit-cutoff test keep
    byte-identical semantics). ``None`` (production) resolves via
    :func:`~brutex.calendars.nse.daily_derive_cutoff` on ``daily_cutoff_now``
    — the run's PINNED IST instant (the orchestrator pins ONE instant per
    prebuild run for parallel byte-identity, DEC-HARDWARE-ENVELOPE-001 M1);
    ``None`` falls back to the injectable ``_now_ist()`` clock seam
    (DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001): a still-forming /
    pre-close session is never written as a final daily bar (no look-ahead),
    while a run on a non-trading day OR provably AFTER the run day's own
    close (per-date close honored — regular, ``SPECIAL_WEEKEND_TRADING_
    SESSIONS`` Budget weekends, Muhurat evenings) includes the run day.
    **Data-grounded arm (DEC-DAILY-CUTOFF-GROUNDED-001):** on that default
    path, for a DENSE symbol (``dense_symbol=True`` — a SPOT INDEX per the
    #1031 gate; an illiquid name's genuine last trade legitimately precedes
    15:29, so per-symbol grounding would wrongly defer its market-complete
    sessions) with :func:`daily_cutoff_grounding_enabled` ON, the cutoff is
    re-grounded AFTER the 1-minute read against the frame's own last bar
    (:func:`~brutex.calendars.nse.daily_derive_cutoff_grounded` — DEFER-only:
    a post-close run over a PROVABLY-incomplete run-day session defers that
    one day instead of finalizing a partial daily bar; a complete session
    — last bar 15:29 + 1 min == the calendar close — derives byte-identically
    to the clock arm). The incremental-skip check below stays on the PRE-read
    clock verdict (the zero-read fast path cannot see the frame), while the
    STAMP records the EFFECTIVE cutoff the derive consumed (see
    :class:`~brutex.data.derive_stamp.DeriveStamp`); a grounded deferral
    therefore honestly re-derives each run until the data completes.
    Rerun-safe: the derived intraday partitions AND the derived daily are
    both latest-wins (DEC-TF-REMAT-LATEST-WINS-001 — a re-derivation of
    identical data is a silent byte-identical no-op, and a bucket recomputed
    from GROWN 1-minute data REPLACES its stale partial-day predecessor
    instead of being keep-first-discarded) — CLAUDE.md §2 Rule 7. The vendor
    1-minute base itself stays keep-first (only DERIVED data is
    recomputable/replaceable).

    DELIBERATE live-edge divergence (DEC-DOCTOR-KIT-HONESTY-001 fix-4 verdict,
    pinned by ``TestPrebuildLiveEdgeDivergencePinned``): the intraday TFs carry
    the RUN DAY's own session when the stored 1-minute contains it, while the
    derived ``1day`` EXCLUDES it (``ist_date >= before`` — the forming-session
    guard above), so a MID-SESSION prebuild run writes exactly ONE fewer daily
    session than the intraday TFs (the operator's 122-vs-123 ADANIENT
    observation). Since DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 the divergence is
    confined to a mid-session run: a POST-CLOSE run derives the run day too
    (superseding the fix-4 "whole-day-granular — the code cannot cheaply prove
    the session closed" clause), and a mid-session run still SELF-HEALS on the
    next pipeline run via the latest-wins derived ``1day``. Partition-row
    counting note: midnight-IST stamping places an IST date-D daily bar in the
    UTC month of D−1 (a Jan-1 bar lives in the December partition) — shifted,
    never missing.

    *months* (DEC-TF-REBUILD-MONTH-BOUND-001): an optional explicit
    ``(year, month)`` bound on the 1-minute READ — ``None`` (the default; the
    pipeline prebuild + doctor call sites) is the byte-identical whole-tree
    read, a set makes a targeted INTRADAY-TF rebuild O(affected-months) instead
    of O(symbol-history) (the lake healer's recall). Sound for the intraday TFs
    because an NSE session never crosses a UTC month boundary (the pinned
    invariant on :func:`month_partition_files`). ``months`` +
    ``include_daily=True`` RAISES :class:`ValueError`: the derived ``1day`` is
    midnight-IST-stamped, so an IST date-D daily bar lives in the UTC month of
    D−1 — a month-bounded daily derive needs the ±1-day straddle pad the daily
    recall path already has (``make_lake_read_1m_window``); month-bounding the
    daily arm here is the NAMED follow-up, not silently mis-derived. A
    month-bounded run additionally DISABLES the incremental skip/stamp below —
    a partial (months-subset) derive must never skip, and must never stamp
    itself as a full derive (DEC-PREBUILD-INCREMENTAL-001 interplay).

    Cost: one O(bars) partition read (month-bounded when *months* is given) +
    one O(bars) resample per timeframe — NOT an O(1)-total claim (Golden Rule
    #10). Returns the per-TF written counts. Raw lake I/O is composed from the
    ``# pragma: no cover`` builders; integration-tested by the round-trip tests.

    **Log-volume envelope (DEC-PREBUILD-LOG-FLOOD-001):** a completed derive
    emits ONE ``lake.derived.write_summary`` INFO per SYMBOL (timeframe count +
    total bars); the per-partition ``lake.partition.write`` telemetry on this
    derived path rides DEBUG (see :func:`make_lake_write_tf`), so a prebuild of
    N partitions costs O(symbols) INFO lines, never O(partitions) — the fetch
    phase's rate/429/settle audit trail can no longer be rotated away by the
    derived fan-out (measured: 205,609 INFO lines ≈ ~26 min to roll the
    10 MiB × 5 rotation, pull 2026-07-04/05).

    **Cross-process derive signal (DEC-ENGINE-MISC-PACK-001):** the WHOLE
    per-symbol materialise runs under the exclusive
    :func:`~brutex.data.derive_lock.derive_write_lock` (the fixed
    ``<lake_root>/.locks/derive.lock`` flock), so a concurrent reader in a
    SEPARATE process — the ``/movers`` stored-first read probing
    :func:`~brutex.data.derive_lock.derive_in_progress` — can never observe a
    half-refreshed TF set as its stored source. Held PER CALL (one symbol) and
    released between symbols, so a multi-symbol prebuild never starves readers
    for its whole duration; lock ORDER is derive → per-partition flock (writer
    only), never the reverse — no cycle.

    **Daily-open substitution observe (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001,
    SPOT + ``include_daily=True`` only):** after the daily derive, the SAME
    in-memory 1-minute frame is scanned (zero extra I/O) for complete sessions
    whose 09:15 IST bar is absent — the derived daily open silently
    substituted the earliest present minute (the implicit ``open.first()``).
    The count rides :attr:`MaterializeResult.opens_substituted`; the guarded
    :func:`~brutex.data.spot_daily_resample.observe_daily_open_substitutions`
    emits the observe-only log events, gated by *dense_symbol* (``True`` = a
    SPOT INDEX per the #1031 ``Instrument.is_index`` gate → per-session WARNs,
    capped; ``False`` = count-only aggregate; ignored for F&O kinds — a
    deep-OTM contract's 09:15-absence is expected illiquidity, so the F&O
    ``resample_fn`` path is deliberately NOT scanned). STORED BARS ARE
    BYTE-UNCHANGED — the scan is read-only and runs AFTER the daily fill
    (golden-pinned).

    **Incremental skip (DEC-PREBUILD-INCREMENTAL-001, per-SYMBOL granularity —
    the bounded first step; a per-TF stamp is explicitly future work):** with
    *incremental* enabled (``None`` → the ``BRUTEX_PREBUILD_INCREMENTAL`` env
    default ON), the symbol's 1-minute source-file token is compared against
    its sidecar stamp (:mod:`brutex.data.derive_stamp`) BEFORE any bar is
    read; an unchanged source + identical request (+ the daily-cutoff rule +
    the derived-dirs-present guard) returns ``unchanged=True`` with ZERO
    read/resample — provably byte-identical lake state, because the full
    re-derive's latest-wins writes rewrite identical values byte-identically
    on unchanged data (golden-pinned). A successful FULL derive writes a fresh stamp
    (best-effort). ``incremental=False`` (the lake-heal rebuild path — heal
    must always rebuild what it just quarantined) never skips and never
    stamps.
    """
    with derive_write_lock(lake_root):
        from datetime import UTC, datetime

        from brutex.calendars.nse import daily_derive_cutoff, daily_derive_cutoff_grounded
        from brutex.data.derive_stamp import (
            DeriveStamp,
            derive_stamp_path,
            incremental_prebuild_enabled,
            load_derive_stamp,
            save_derive_stamp,
            should_skip_derive,
            source_1m_token,
        )

        if months is not None and include_daily:
            raise ValueError(
                "months= cannot be combined with include_daily=True — the midnight-IST "
                "derived 1day straddles UTC months (use the daily recall path's "
                "±1-day-padded window read instead; DEC-TF-REBUILD-MONTH-BOUND-001)"
            )
        requested = tuple(dict.fromkeys(tfs))
        # Resolve the daily forming-session cutoff up-front — the skip rule must
        # see the SAME value the derive would use (resolved unconditionally so the
        # daily branch below needs no Optional narrowing; a non-daily run simply
        # never uses it and its stamp records daily_before=None).
        # DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001: the production default is the
        # session-close-aware cutoff — a post-close (or non-trading-day) run
        # includes the run day's completed session; a mid-session run excludes it.
        # ``cutoff_now`` stays None iff the caller passed an EXPLICIT cutoff —
        # the "no grounding, daily_before is LAW" marker for the daily branch
        # below (DEC-DAILY-CUTOFF-GROUNDED-001).
        cutoff_now: datetime | None = None
        if daily_before is not None:
            before: date = daily_before
        else:
            cutoff_now = daily_cutoff_now if daily_cutoff_now is not None else _now_ist()
            before = daily_derive_cutoff(cutoff_now)
        before_iso = before.isoformat() if include_daily else None

        # A months-bounded (partial) derive never skips and never stamps
        # (DEC-TF-REBUILD-MONTH-BOUND-001 × DEC-PREBUILD-INCREMENTAL-001).
        if months is not None:
            inc = False
        else:
            inc = incremental_prebuild_enabled() if incremental is None else incremental
        token: str | None = None
        stamp_file = derive_stamp_path(lake_root, exchange, segment, symbol)
        if inc:
            # Token BEFORE the read (conservative: a write landing between the
            # token and the read makes the NEXT run re-derive, never a stale skip).
            token = source_1m_token(lake_root, exchange, segment, symbol)
            if (
                token is not None
                and should_skip_derive(
                    load_derive_stamp(stamp_file),
                    token=token,
                    tfs=requested,
                    include_daily=include_daily,
                    daily_before=before_iso,
                )
                and _derived_dirs_present(
                    lake_root, exchange, segment, symbol, requested, include_daily
                )
            ):
                _LOG.info(
                    "prebuild.derive.unchanged_skip",
                    exchange=exchange,
                    segment=segment,
                    symbol=symbol,
                    include_daily=include_daily,
                )
                return MaterializeResult(written={}, skipped=(), unchanged=True)

        read_1m = make_lake_read_1m(
            lake_root=lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            with_open_interest=kind is not PrebuildKind.SPOT,
            months=months,
        )
        write_tf = make_lake_write_tf(
            lake_root=lake_root, exchange=exchange, segment=segment, symbol=symbol
        )
        resampler = intraday_resampler_for(kind)

        def _stamp(last_1m_ist_date: str | None, daily_before_iso: str | None) -> None:
            """Record the successful full derive (best-effort — never raises).

            ``daily_before_iso`` is the EFFECTIVE cutoff the derive consumed —
            a grounded deferral stamps the deferred value, never the clock one
            (DEC-DAILY-CUTOFF-GROUNDED-001; see ``DeriveStamp.daily_before``).
            """
            if not inc or token is None:
                return
            save_derive_stamp(
                stamp_file,
                DeriveStamp(
                    token=token,
                    tfs=tuple(sorted(set(requested))),
                    include_daily=include_daily,
                    daily_before=daily_before_iso,
                    last_1m_ist_date=last_1m_ist_date,
                    written_at=datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )

        def _log_write_summary(result: MaterializeResult) -> None:
            """ONE aggregate INFO per materialised SYMBOL (DEC-PREBUILD-LOG-FLOOD-001).

            The per-partition ``lake.partition.write`` telemetry is DEBUG on this
            derived path (see ``make_lake_write_tf``), so the prebuild's audit
            record survives log rotation in O(symbols) not O(partitions): one line
            per symbol carrying the timeframe count + total derived bars — the
            aggregate form of the per-partition fields the forensics rely on.
            """
            _LOG.info(
                "lake.derived.write_summary",
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                timeframes_written=len(result.written),
                total_bars=result.total_bars,
                skipped=len(result.skipped),
                include_daily=include_daily,
            )

        if not include_daily:
            result = materialize_timeframes(
                tfs=requested, read_1m=read_1m, write_tf=write_tf, resampler=resampler
            )
            _stamp(last_1m_ist_date=None, daily_before_iso=before_iso)
            _log_write_summary(result)
            return result

        from brutex.data.spot_daily_resample import (
            fill_daily_from_minute,
            make_lake_write_1day,
            observe_daily_open_substitutions,
        )

        # Read the 1-minute base ONCE and share the frame between the intraday
        # derivation and the daily derivation (halves the partition I/O).
        df_1m = read_1m()
        # ONE last-bar extraction, two consumers: the grounded cutoff below and
        # the incremental stamp's daily-cutoff rule (DEC-DAILY-CUTOFF-GROUNDED-001).
        last_bar_ist = _last_ist_datetime(df_1m)
        # Data-grounded cutoff arm (DEC-DAILY-CUTOFF-GROUNDED-001): only on the
        # default-cutoff path (``cutoff_now`` set ⇔ no explicit ``daily_before``),
        # only for a dense (index) symbol, and only while the env gate is ON.
        # DEFER-only: it can demote the clock's "run day closed" verdict when the
        # frame's own last bar proves the run-day session INCOMPLETE, never the
        # reverse — so a complete regular session (last bar 15:29) derives
        # byte-identically to the clock arm.
        effective_before = before
        if cutoff_now is not None and dense_symbol and daily_cutoff_grounding_enabled():
            if last_bar_ist is not None and last_bar_ist.date() > cutoff_now.date():
                # Observe-only anomaly: a FUTURE-dated bar escaped the pull-side
                # drop_bars_after guard (clock skew / corruption). The grounded
                # arm abstains to the clock verdict; the prebuild never crashes
                # on a diagnostic.
                _LOG.warning(
                    "prebuild.daily_cutoff.future_bar_anomaly",
                    symbol=symbol,
                    last_bar_ist=last_bar_ist.isoformat(),
                    run_day=cutoff_now.date().isoformat(),
                )
            grounded = daily_derive_cutoff_grounded(cutoff_now, last_bar_ist=last_bar_ist)
            if grounded != before:
                _LOG.info(
                    "prebuild.daily_cutoff.grounded_defer",
                    symbol=symbol,
                    last_bar_ist=None if last_bar_ist is None else last_bar_ist.isoformat(),
                    clock_before=before.isoformat(),
                    grounded_before=grounded.isoformat(),
                )
            effective_before = grounded
        intraday = materialize_timeframes(
            tfs=requested, read_1m=lambda: df_1m, write_tf=write_tf, resampler=resampler
        )
        daily = fill_daily_from_minute(
            read_1m=lambda: df_1m,
            write_1day=make_lake_write_1day(
                lake_root=lake_root, exchange=exchange, segment=segment, symbol=symbol
            ),
            before=effective_before,
            resample_fn=daily_resample_fn_for(kind),
        )
        # OBSERVE-ONLY daily-open substitution scan (DEC-CALENDAR-EXPECTED-BARS-
        # DAILY-OPEN-001) — SPOT only, AFTER the fill so a fill exception short-circuits
        # identically to before; read-only over the frame already in memory, so
        # the written bars are byte-unchanged (golden-pinned). Guarded inside:
        # a scan error logs + returns 0, never failing the prebuild.
        opens_substituted = 0
        if kind is PrebuildKind.SPOT:
            opens_substituted = observe_daily_open_substitutions(
                df_1m, before=effective_before, symbol=symbol, dense=dense_symbol
            )
        _stamp(
            last_1m_ist_date=None if last_bar_ist is None else last_bar_ist.date().isoformat(),
            daily_before_iso=effective_before.isoformat(),
        )
        result = MaterializeResult(
            written={**intraday.written, DAILY_TF_LABEL: daily.bars_written},
            skipped=intraday.skipped,
            opens_substituted=opens_substituted,
        )
        _log_write_summary(result)
        return result


def _last_ist_datetime(df_1m: pl.DataFrame) -> datetime | None:
    """The frame's last bar's full IST timestamp, or ``None`` when empty.

    The ONE last-bar extraction (one ``max()`` scan, two consumers —
    DEC-DAILY-CUTOFF-GROUNDED-001): the data-grounded cutoff arm
    (:func:`~brutex.calendars.nse.daily_derive_cutoff_grounded`) consumes the
    full instant; the stamp's daily-cutoff rule (DEC-PREBUILD-INCREMENTAL-001)
    consumes its calendar date via :func:`_last_ist_date`. A naive (tz-less)
    timestamp is treated as UTC — the lake's storage convention; a non-datetime
    ``max()`` (defensive) reads ``None`` — honest unknown, never fabricated.
    """
    from datetime import UTC, datetime

    from brutex.calendars.nse import IST

    if df_1m.height == 0:
        return None
    ts = df_1m.get_column("ts_utc").max()
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(IST)


def _last_ist_date(df_1m: pl.DataFrame) -> str | None:
    """The frame's last bar's IST calendar date (ISO), or ``None`` when empty.

    Feeds the stamp's daily-cutoff rule (DEC-PREBUILD-INCREMENTAL-001): a
    stamped run whose cutoff already lay strictly AFTER this date finalised
    every session in the data, so a later cutoff derives identically from the
    unchanged 1-minute source. Delegates to :func:`_last_ist_datetime` — one
    extraction, one convention (DEC-DAILY-CUTOFF-GROUNDED-001).
    """
    last = _last_ist_datetime(df_1m)
    return None if last is None else last.date().isoformat()


class NoStored1mBarsError(ValueError):
    """The symbol's WHOLE 1-minute partition holds ZERO stored parquet files.

    DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001: subclasses :class:`ValueError` so
    every existing catcher (``gated_prebuild._per_symbol_data_errors``, the
    doctor/preflight callers, every ``pytest.raises(ValueError)`` pin) is
    behavior-identical; the prebuild's SPOT phase catches it FIRST to classify
    the vendor-empty / never-pulled shape as ``no_source`` instead of a
    warm-withholding failure. Raised ONLY by the whole-partition arm of
    :func:`make_lake_read_1m` — the months-bounded arm (the heal-recall path)
    keeps plain ``ValueError``: a months-scoped absence is NOT vendor-empty.
    """


def spot_1m_partition_dir(lake_root: Path, exchange: str, segment: str, symbol: str) -> Path:
    """The symbol's 1-minute lake partition dir — the ONE base-path law (§11 COMMON).

    DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001: extracted from
    :func:`make_lake_read_1m`'s inline expression so the reader and every
    probe consumer share one spelling of ``bars/<exch>/<seg>/<sym>/1minute``.
    """
    return lake_root / "bars" / exchange / segment / symbol / "1minute"


def _raise_partition_scan_error(error: OSError) -> None:
    """Propagate a partition-enumeration fault out of :func:`has_stored_1m_bars`.

    The ``os.walk(onerror=...)`` hook (refuter round 4): a fault while
    LISTING the partition must never read as "the partition is empty" —
    raise it so the reader classifies the symbol a per-symbol FAILURE
    (``OSError`` is a :func:`~brutex.data.errors.parquet_read_skip_errors`
    member), never the benign ``no_source``.
    """
    raise error


def has_stored_1m_bars(lake_root: Path, exchange: str, segment: str, symbol: str) -> bool:
    """``True`` iff the partition holds >=1 stored 1-minute parquet file — lake truth NOW.

    The EXACT existence predicate :func:`make_lake_read_1m`'s whole-tree arm
    gates on (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001 — ONE predicate for the
    reader AND every future probe consumer; §11 COMMON). O(dir entries),
    short-circuits at the first parquet-named entry (file OR directory —
    ``rglob``-parity, so a dataset-style ``01.parquet/`` dir plant keeps its
    pre-change verdict); ZERO Parquet opens. Deliberately blind to WHY the
    partition is empty (vendor-empty vs never-pulled vs heal-emptied) and to
    a 0-ROW parquet: a hand-planted schema-complete 0-row file passes here
    and the prebuild then COMPLETES the symbol as a 0-bar SUCCESS (neither
    no_source nor failed; warm unaffected) — EXECUTED, byte-identical to
    main's pre-change behavior on the same plant (refuter round 3, pinned by
    ``test_e2e_zero_row_parquet_completes_as_zero_bar_success``). The shape
    is out-of-band — production writers never render 0-row files and the
    #1594 heal quarantine-MOVES a wholly-removed partition — while a
    schema-BROKEN plant still fails the prebuild loudly via the per-symbol
    data-error quarantine (the round-1 R2 executed ComputeError pin).

    Fail-LOUD on an UNENUMERABLE partition (refuter round 4): CPython
    pathlib SUPPRESSES ``PermissionError`` during glob traversal (and
    ``Path.is_dir()`` swallows ``OSError``), so the original ``is_dir() and
    any(rglob("*.parquet"))`` spelling read a permission-broken POPULATED
    partition (chmod/ownership mishap on one symbol's subtree) as *empty* —
    reclassifying a real fault as the benign ``no_source`` whose
    "vendor-empty or never pulled" wording named two FALSE causes, with
    ``ok=True`` and the movers warm firing (executed A/B: the merge-base
    classified the identical lake state a loud warm-withholding FAILURE).
    The probe therefore enumerates with an OSError-PROPAGATING walk
    (:func:`_raise_partition_scan_error`): an ``EACCES`` — base dir OR any
    subtree level — propagates out of the reader as ``OSError``, a
    :func:`~brutex.data.errors.parquet_read_skip_errors` member, restoring
    the merge-base per-symbol-FAILURE classification (WARNING, ``ok=False``,
    warm withheld). An ABSENT partition — or a non-directory at/inside the
    path (an out-of-band hand-planted shape: nothing can be stored under a
    file) — still reads ``False``, the benign shape.
    """
    base = spot_1m_partition_dir(lake_root, exchange, segment, symbol)
    try:
        if not stat_module.S_ISDIR(os.stat(base).st_mode):
            return False
    except (FileNotFoundError, NotADirectoryError):
        return False
    for _dirpath, dirnames, filenames in os.walk(base, onerror=_raise_partition_scan_error):
        if any(name.endswith(".parquet") for name in chain(dirnames, filenames)):
            return True
    return False


__all__ = [
    "DAILY_CUTOFF_GROUNDED_ENV",
    "DAILY_TF_LABEL",
    "INTRADAY_TF_INTERVALS",
    "MATERIALIZABLE_TFS",
    "MaterializeResult",
    "NoStored1mBarsError",
    "PrebuildKind",
    "daily_cutoff_grounding_enabled",
    "daily_resample_fn_for",
    "frame_to_candle_bars",
    "has_stored_1m_bars",
    "intraday_resampler_for",
    "make_lake_read_1m",
    "make_lake_write_tf",
    "materialize_lake_timeframes",
    "materialize_timeframes",
    "month_partition_files",
    "prebuild_kind_for",
    "spot_1m_partition_dir",
]


def make_lake_read_1m(  # pragma: no cover — raw lake read, integration-tested (DEC-TF-MATERIALIZE-001)
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    with_open_interest: bool = False,
    months: Iterable[tuple[int, int]] | None = None,
) -> Callable[[], pl.DataFrame]:
    """Build the injected 1-minute reader for :func:`materialize_timeframes`.

    Returns a zero-arg callable that reads the symbol's stored ``1minute``
    Parquet partitions into a sorted :class:`polars.DataFrame` with the columns
    the resampler requires (``ts_utc, open, high, low, close, volume``; plus
    ``open_interest`` when *with_open_interest* is set, for the OI-aware F&O
    futures path — DEC-PREBUILD-SELECT-001). Raw lake I/O — ``# pragma: no
    cover``; exercised by ``tests/integration/test_timeframe_materialize_roundtrip.py``.
    Moved here from the dashboard route glue (DEC-TF-MATERIALIZE-002) so the
    manual route AND the pull-settle auto-derive share ONE builder
    (CLAUDE.md §11 COMMON).

    *months* (DEC-TF-REBUILD-MONTH-BOUND-001): an optional explicit
    ``(year, month)`` set. ``None`` (the default — every existing caller) is
    the byte-identical whole-tree ``/**/*.parquet`` read; a set bounds the read
    to ONLY those existing month partitions (via the pure
    :func:`month_partition_files`), so a targeted rebuild is
    O(affected-months), not O(symbol-history). The "no stored 1minute bars"
    :class:`ValueError` contract is kept in BOTH modes (a months set matching
    nothing on disk raises, mirroring the empty-tree raise).

    Price-sanity gate (DEC-STORED-DATA-VALIDATION-001; finiteness added by
    DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001): the collected frame is
    validated by the COMMON
    :func:`~brutex.data.price_sanity.validate_frame_price_sanity` (finiteness
    + positivity + OHLC ordering, one vectorized pass) BEFORE it reaches any
    resampler — a corrupt 1m bar (e.g. ``high < low``, or a planted
    ``high = inf``/``NaN`` the pre-fix mask was blind to) previously LAUNDERED
    into self-consistent-looking derived buckets across all 19 TFs + ``1day``
    (live-reproduced: an emitted derived ``low: 0.0``; the assurance session's
    executed repro: a derived 5-minute bucket ``high = inf``). The raised
    :class:`~brutex.data.errors.ResampleInputError` is already in the
    prebuild's per-symbol quarantine set, so the symbol is quarantined +
    NAMED and its derived TFs are NOT written — rows are never silently
    dropped or "fixed".

    Timestamp-range gate (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001): BEFORE the
    price gate the collected frame's ``ts_utc`` is validated by the COMMON
    :func:`~brutex.data.price_sanity.validate_frame_ts_datetime_range` — a
    stored out-of-``datetime``-range timestamp (e.g. a planted ``2**62``-us
    stamp) survives the vectorized resample and then PANICS the Rust→Python
    conversion (``pyo3_runtime.PanicException``, a BaseException) at
    ``frame_to_candle_bars``'s ``to_list()``, past every ``except Exception``
    per-symbol quarantine (executed-repro'd: ONE poisoned symbol aborted the
    WHOLE crossverify prebuild leg). The gate raises the SAME quarantining
    :class:`~brutex.data.errors.ResampleInputError` deterministically at the
    read boundary instead; clean frames pay one vectorized i64 min/max pass.

    Bad-tick screen (DEC-READ-GUARD-PARITY-001): after the sanity gate the
    frame is screened by :func:`~brutex.data.bad_tick.screen_frame_bad_ticks`
    (the engine read boundary's screen, frame form — same 4.0x threshold, same
    ``BRUTEX_BAD_TICK_SCREEN`` kill switch, one ``feed.bad_tick_skipped`` WARN
    per session with a ``prebuild:``-prefixed source), so a self-consistent
    scaled 1m spike is dropped-and-counted before it can poison the derived
    bucket HIGH/LOW or the ``1day`` anchor. Clean frames are returned
    object-identical (zero-copy); clean lakes stay byte-identical.
    """
    import polars as pl

    from brutex.data.bad_tick import bad_tick_screen_enabled, screen_frame_bad_ticks
    from brutex.data.price_sanity import (
        validate_frame_price_sanity,
        validate_frame_ts_datetime_range,
    )

    columns: tuple[str, ...] = ("ts_utc", "open", "high", "low", "close", "volume")
    if with_open_interest:
        columns = (*columns, "open_interest")

    def _read() -> pl.DataFrame:
        base = spot_1m_partition_dir(lake_root, exchange, segment, symbol)
        if months is not None:
            # Month-bounded read (DEC-TF-REBUILD-MONTH-BOUND-001): the explicit
            # existing-file list replaces the whole-tree glob. Sound because an
            # NSE session never crosses a UTC month (see month_partition_files).
            # Deliberately plain ValueError, NEVER NoStored1mBarsError
            # (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001): a months-scoped
            # absence (the heal-recall path) says nothing about the WHOLE
            # partition — it must stay a quarantining prebuild failure.
            source: str | list[str] = month_partition_files(base, months)
            if not source:
                raise ValueError(
                    f"no stored 1minute bars for {exchange}/{segment}/{symbol} in the "
                    "requested months — pull the 1-minute first, then derive timeframes"
                )
        elif has_stored_1m_bars(lake_root, exchange, segment, symbol):
            source = base.as_posix() + "/**/*.parquet"
        else:
            # Shape-matched remediation (refuter round 1, r3 F-2 — the #1649a
            # precedent): this reader cannot tell WHY the partition is empty,
            # and "pull the spot first" is the WRONG advice for a symbol the
            # vendor cleanly returned NO data for (settled EMPTY — there is
            # nothing to pull), so BOTH candidate causes are named. The typed
            # subclass (message bytes UNCHANGED) lets the prebuild's spot
            # phase classify this shape as ``no_source`` instead of a
            # warm-withholding failure (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001).
            raise NoStored1mBarsError(
                f"no stored 1minute bars for {exchange}/{segment}/{symbol} — "
                "nothing to derive: if the 1-minute spot was never pulled, "
                "pull it first, then derive timeframes; if the vendor cleanly "
                "returned no data for this symbol (windows settled EMPTY), "
                "there is nothing to derive — no action needed"
            )
        # Schema-tolerant tree read (DEC-GREEKS-ENGINE-PR2B-001): an enriched
        # OPTION contract's month files may mix the raw 7-column and the
        # Greeks-enriched 17-column shape (plan §15.3); only the base
        # OHLCV(+OI) columns are selected below, which every shape carries —
        # byte-identical for a uniform tree.
        df = pl.scan_parquet(
            source,
            missing_columns="insert",
            extra_columns="ignore",
            # LITERAL month-file lists never glob (DEC-SPOT-DERIVE-TS-RANGE-
            # HARDENING-001, the #1594 round-15 metachar law on the bars tree):
            # a symbol dir named `NSE-X[12]` beside real `NSE-X1`/`NSE-X2`
            # siblings otherwise glob-reads the SIBLINGS' bars into this
            # symbol. The whole-tree branch is deliberate `**` discovery and
            # keeps glob; the writer additionally refuses to mint such dirs
            # (partition._FORBIDDEN_SYMBOL_CHARS gains `[`/`]`).
            glob=isinstance(source, str),
        ).collect()
        frame = df.rename({"timestamp": "ts_utc"}).select(*columns).sort("ts_utc")
        context = f"{exchange}/{segment}/{symbol}/1minute"
        # DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001: refuse an out-of-datetime-
        # range stored timestamp FIRST (a 2**62-us stamp panics the Rust→Python
        # conversion downstream — a BaseException past every except Exception;
        # the price gate's own first-offender message indexes ts too). Raises
        # the quarantining ResampleInputError.
        validate_frame_ts_datetime_range(frame, ts_column="ts_utc", context=context)
        # DEC-STORED-DATA-VALIDATION-001: refuse to feed a price-insane 1m
        # frame to the resamplers (raises the quarantining ResampleInputError).
        validate_frame_price_sanity(frame, ts_column="ts_utc", context=context)
        # DEC-READ-GUARD-PARITY-001: drop x10/÷10 scaled 1m spikes (the engine
        # read boundary's BadTickScreen, frame form) BEFORE any resampler — a
        # self-consistent scaled bar passes price sanity and otherwise poisons
        # the derived bucket HIGH/LOW + the 1day anchor. Runs AFTER the sanity
        # gate (is_bad_tick assumes positivity). Default ON; the ONE
        # BRUTEX_BAD_TICK_SCREEN kill switch flips both boundaries together.
        if bad_tick_screen_enabled():
            frame = screen_frame_bad_ticks(frame, ts_column="ts_utc", source=f"prebuild:{context}")
        return frame

    return _read


def make_lake_write_tf(  # pragma: no cover — raw lake write, integration-tested (DEC-TF-MATERIALIZE-001)
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
) -> Callable[[str, pl.DataFrame], int]:
    """Build the injected per-timeframe writer for :func:`materialize_timeframes`.

    Returns a ``(tf_label, frame) -> bars_written`` callable that converts the
    resampled polars frame to :class:`~brutex.data.models.CandleBar` objects
    via the BATCHED :func:`frame_to_candle_bars`
    (DEC-PREBUILD-GIL-VECTORIZE-001 — byte-identical to the former per-row
    ``iter_rows`` loop; see that function's docstring for the starvation
    mechanism + honest envelope) and appends them to the lake via a
    :class:`~brutex.data.sink.ParquetBarSink` under the TF's own stored interval
    partition (:data:`INTRADAY_TF_INTERVALS`). ``open_interest`` is carried from
    the resampled frame when the OI-aware F&O futures resampler emitted it
    (DEC-PREBUILD-SELECT-001), else ``0`` (the spot convention — spot frames
    have no OI column). A present-but-NULL OI bucket on an OI-carrying frame
    is stored ``None`` (DEC-PREOPEN-DAY-FRESHNESS-001, the B-3 fold — ALIGNED
    with the F&O DAILY derive, whose
    :func:`~brutex.data.fno_daily_resample.resample_1m_to_fno_daily` stores
    ``None`` for a null carry-last OI, and with the canonical resampler's own
    "a null OI source bar yields a null bucket OI — preserved, NEVER
    fabricated as 0" law; the former unconditional default fabricated a
    stored ``0`` — an un-ledgered divergence, /db-display-visible,
    FUT-reachable since DEC-OPTIONS-PREBUILD-RETIRE-001 retired the options
    derived TFs. Already-stored ``0``s stay inert until the next latest-wins
    re-materialise — derived partitions are recomputable caches). The sink
    snaps prices to the 2-decimal tick grid + merges idempotently by
    ``(year, month)`` (CLAUDE.md §7 / DEC-SPOT-PRICE-2DP-001) in
    **latest-wins** mode (DEC-TF-REMAT-LATEST-WINS-001 — a re-materialised
    bucket recomputed from grown 1-minute data REPLACES its stale partial-day
    predecessor; identical data rewrites byte-identically; the vendor 1-minute
    base never routes through this writer and stays keep-first). The write
    itself stays atomic tmp → fsync → ``os.replace`` via the shared
    :func:`~brutex.data.atomic_io.atomic_write_via` inside the writer. Raw
    lake I/O — ``# pragma: no cover``; exercised by the round-trip integration
    test. Moved here from the dashboard route glue (DEC-TF-MATERIALIZE-002) —
    one builder for every caller.
    """
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    # DERIVED-TF writer → per-partition `lake.partition.write` telemetry at
    # DEBUG (DEC-PREBUILD-LOG-FLOOD-001): the prebuild writes ~10⁵-10⁶ tiny
    # derived partitions per run — at INFO those lines (~132/s measured,
    # 205,609 of 206,606 retained lines) rotated the 10 MiB × 5 log over in
    # ~26 min and WIPED the fetch phase's rate/429/settle audit trail. The
    # fields survive per-SYMBOL in the `lake.derived.write_summary` INFO
    # (materialize_lake_timeframes); the live-pull vendor writers keep INFO.
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root, partition_log_level="debug"))

    def _write(tf_label: str, frame: pl.DataFrame) -> int:
        interval = INTRADAY_TF_INTERVALS[tf_label]
        result = sink.append(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            # OI-carrying (F&O) frames keep a null bucket OI as stored None —
            # the F&O daily derive's law (DEC-PREOPEN-DAY-FRESHNESS-001 B-3
            # fold; see the docstring). Spot frames (no OI column) keep the
            # legacy stored-0 convention byte-identically.
            bars=frame_to_candle_bars(
                frame,
                absent_open_interest=None if "open_interest" in frame.columns else 0,
            ),
            # LATEST-WINS on the DERIVED intraday partitions (DEC-TF-REMAT-LATEST-WINS-001,
            # mirroring the derived ``1day``'s make_lake_write_1day): a re-materialise
            # recomputes every bucket from the CURRENT stored 1-minute, so the fresh
            # bucket is authoritative — keep-first would FREEZE a partial-day bucket
            # materialised mid-day (e.g. a 15m bucket derived from only its first 3
            # minutes) forever, discarding every later correct recompute. Derived data
            # is recomputable, so replacing it is safe ("keep-first would keep poison",
            # DEC-LAKE-HEAL-001); the VENDOR 1-minute base is written elsewhere and
            # STAYS keep-first (never routed through this writer — regression-pinned).
            # Idempotent: re-deriving unchanged data writes identical values, a silent
            # byte-identical no-op (CLAUDE.md §2 Rule 7, golden-pinned).
            merge_mode="latest-wins",
        )
        return result.bars_appended

    return _write
