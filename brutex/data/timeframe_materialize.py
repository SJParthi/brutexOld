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

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

import structlog

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
    from collections.abc import Callable, Iterable
    from datetime import date
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
        envelope (Golden Rule #10): once all 19 intraday + the ``1day``
        partitions exist, the options lake costs **~2-3× its 1-minute
        footprint** — an operator-accepted cost of the decree.
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
      design; honest cost: a fully-materialised options lake is **~2-3× its
      1-minute footprint**, operator-accepted);
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

    ``daily_before`` is the forming-session cutoff (an IST date); ``None``
    (production) resolves to today IST, so a still-forming session is never
    written as a final daily bar (no look-ahead). Rerun-safe: the derived
    intraday partitions AND the derived daily are both latest-wins
    (DEC-TF-REMAT-LATEST-WINS-001 — a re-derivation of identical data is a
    silent byte-identical no-op, and a bucket recomputed from GROWN 1-minute
    data REPLACES its stale partial-day predecessor instead of being
    keep-first-discarded) — CLAUDE.md §2 Rule 7. The vendor 1-minute base
    itself stays keep-first (only DERIVED data is recomputable/replaceable).

    DELIBERATE live-edge divergence (DEC-DOCTOR-KIT-HONESTY-001 fix-4 verdict,
    pinned by ``TestPrebuildLiveEdgeDivergencePinned``): the intraday TFs carry
    the RUN DAY's own session when the stored 1-minute contains it, while the
    derived ``1day`` EXCLUDES it (``ist_date >= before`` — the forming-session
    guard above), so such a prebuild run writes exactly ONE fewer daily session
    than the intraday TFs (the operator's 122-vs-123 ADANIENT observation).
    The guard is whole-day-granular — even a post-close run excludes the run
    day (the code cannot cheaply prove the session closed) — and SELF-HEALS on
    the next pipeline run via the latest-wins derived ``1day``. Partition-row
    counting note: midnight-IST stamping places an IST date-D daily bar in the
    UTC month of D−1 (a Jan-1 bar lives in the December partition) — shifted,
    never missing.

    Cost: one O(bars) partition read + one O(bars) resample per timeframe —
    NOT an O(1)-total claim (Golden Rule #10). Returns the per-TF written
    counts. Raw lake I/O is composed from the ``# pragma: no cover`` builders;
    integration-tested by the round-trip tests.

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
    from datetime import UTC, datetime

    from brutex.calendars.nse import IST
    from brutex.data.derive_stamp import (
        DeriveStamp,
        derive_stamp_path,
        incremental_prebuild_enabled,
        load_derive_stamp,
        save_derive_stamp,
        should_skip_derive,
        source_1m_token,
    )

    requested = tuple(dict.fromkeys(tfs))
    # Resolve the daily forming-session cutoff up-front — the skip rule must
    # see the SAME value the derive would use (resolved unconditionally so the
    # daily branch below needs no Optional narrowing; a non-daily run simply
    # never uses it and its stamp records daily_before=None).
    before: date = (
        daily_before if daily_before is not None else datetime.now(UTC).astimezone(IST).date()
    )
    before_iso = before.isoformat() if include_daily else None

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
    )
    write_tf = make_lake_write_tf(
        lake_root=lake_root, exchange=exchange, segment=segment, symbol=symbol
    )
    resampler = intraday_resampler_for(kind)

    def _stamp(last_1m_ist_date: str | None) -> None:
        """Record the successful full derive (best-effort — never raises)."""
        if not inc or token is None:
            return
        save_derive_stamp(
            stamp_file,
            DeriveStamp(
                token=token,
                tfs=tuple(sorted(set(requested))),
                include_daily=include_daily,
                daily_before=before_iso,
                last_1m_ist_date=last_1m_ist_date,
                written_at=datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )

    if not include_daily:
        result = materialize_timeframes(
            tfs=requested, read_1m=read_1m, write_tf=write_tf, resampler=resampler
        )
        _stamp(last_1m_ist_date=None)
        return result

    from brutex.data.spot_daily_resample import (
        fill_daily_from_minute,
        make_lake_write_1day,
        observe_daily_open_substitutions,
    )

    # Read the 1-minute base ONCE and share the frame between the intraday
    # derivation and the daily derivation (halves the partition I/O).
    df_1m = read_1m()
    intraday = materialize_timeframes(
        tfs=requested, read_1m=lambda: df_1m, write_tf=write_tf, resampler=resampler
    )
    daily = fill_daily_from_minute(
        read_1m=lambda: df_1m,
        write_1day=make_lake_write_1day(
            lake_root=lake_root, exchange=exchange, segment=segment, symbol=symbol
        ),
        before=before,
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
            df_1m, before=before, symbol=symbol, dense=dense_symbol
        )
    _stamp(last_1m_ist_date=_last_ist_date(df_1m))
    return MaterializeResult(
        written={**intraday.written, DAILY_TF_LABEL: daily.bars_written},
        skipped=intraday.skipped,
        opens_substituted=opens_substituted,
    )


def _last_ist_date(df_1m: pl.DataFrame) -> str | None:
    """The frame's last bar's IST calendar date (ISO), or ``None`` when empty.

    Feeds the stamp's daily-cutoff rule (DEC-PREBUILD-INCREMENTAL-001): a
    stamped run whose cutoff already lay strictly AFTER this date finalised
    every session in the data, so a later cutoff derives identically from the
    unchanged 1-minute source. A naive (tz-less) timestamp is treated as UTC —
    the lake's storage convention.
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
    return ts.astimezone(IST).date().isoformat()


__all__ = [
    "DAILY_TF_LABEL",
    "INTRADAY_TF_INTERVALS",
    "MATERIALIZABLE_TFS",
    "MaterializeResult",
    "PrebuildKind",
    "daily_resample_fn_for",
    "frame_to_candle_bars",
    "intraday_resampler_for",
    "make_lake_read_1m",
    "make_lake_write_tf",
    "materialize_lake_timeframes",
    "materialize_timeframes",
    "prebuild_kind_for",
]


def make_lake_read_1m(  # pragma: no cover — raw lake read, integration-tested (DEC-TF-MATERIALIZE-001)
    *,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    with_open_interest: bool = False,
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
    """
    import polars as pl

    columns: tuple[str, ...] = ("ts_utc", "open", "high", "low", "close", "volume")
    if with_open_interest:
        columns = (*columns, "open_interest")

    def _read() -> pl.DataFrame:
        base = lake_root / "bars" / exchange / segment / symbol / "1minute"
        if not (base.is_dir() and any(base.rglob("*.parquet"))):
            raise ValueError(
                f"no stored 1minute bars for {exchange}/{segment}/{symbol} — "
                "pull the 1-minute spot first, then derive timeframes"
            )
        # Schema-tolerant tree read (DEC-GREEKS-ENGINE-PR2B-001): an enriched
        # OPTION contract's month files may mix the raw 7-column and the
        # Greeks-enriched 17-column shape (plan §15.3); only the base
        # OHLCV(+OI) columns are selected below, which every shape carries —
        # byte-identical for a uniform tree.
        df = pl.scan_parquet(
            base.as_posix() + "/**/*.parquet",
            missing_columns="insert",
            extra_columns="ignore",
        ).collect()
        return df.rename({"timestamp": "ts_utc"}).select(*columns).sort("ts_utc")

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
    have no OI column; a present-but-null OI bucket also maps to 0). The sink
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

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))

    def _write(tf_label: str, frame: pl.DataFrame) -> int:
        interval = INTRADAY_TF_INTERVALS[tf_label]
        result = sink.append(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=frame_to_candle_bars(frame),
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
