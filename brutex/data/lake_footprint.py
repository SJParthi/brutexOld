"""Measured lake-footprint multiplier + the prebuild's pre-run disk guard.

DEC-LAKE-FOOTPRINT-MEASURED-001. The pipeline's automatic FINAL prebuild phase
(DEC-PIPELINE-AUTOPREBUILD-001, MIGRATION.md #1145) materialises the 19 derived
intraday timeframes + the derived ``1day`` for SPOT + FUTURES + OPTIONS. Its
storage cost was stated as a HAND-WAVED "~2-3× the 1-minute footprint" — an
estimate that IGNORED the Greeks enrichment pass
(:mod:`brutex.data.greeks_enrich`, DEC-GREEKS-ENGINE-PR2B-001), which REWRITES
every option 1-minute partition from the raw 7-column shape to the 17-column
Greeks-enriched shape, pushing the realistic all-in multiplier toward ~5-6×.
This module replaces the guess with MEASUREMENT + a GUARD:

* :func:`measure_lake_footprint` — ONE bounded stat-walk of ``<lake>/bars``
  splitting every partition file's bytes into SOURCE (the ``1minute`` interval
  directories) vs DERIVED (every other stored interval — the 19 intraday TFs +
  ``1day``), per segment (SPOT-side vs FNO) and overall. The OBSERVED
  multiplier is ``total ÷ source`` — directly comparable to the "N× its
  1-minute footprint" language every prebuild surface uses. Because it measures
  the REAL on-disk bytes, the Greeks-enriched 17-column 1-minute rewrite is
  IN the denominator it inflates — the measurement can never repeat the old
  under-estimate for future derived growth.
* :func:`persist_footprint` / :func:`load_observed_multiplier` — the
  ``schema_meta`` persistence (the SAME key-value sidecar pattern as
  DEC-FNO-CANDLE-LAKE-001 #861 / DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001 #875:
  namespaced ``lake_footprint.*`` keys, values as strings), stamping
  ``measured_at`` + the sample byte counts so a later reader can audit the
  observation.
* :func:`evaluate_disk_guard` + :func:`make_disk_guard` — the PRE-RUN disk
  guard the prebuild phase calls BEFORE materialising anything:
  ``estimated_total = source_1m_bytes × multiplier`` (the LAST observed
  multiplier from ``schema_meta``, else the conservative
  :data:`FALLBACK_MULTIPLIER` = 6.5×), ``estimated_need = estimated_total −
  bytes already on disk`` (source + already-materialised derived — both are
  sunk cost, not future need), and the prebuild is SKIPPED gracefully when the
  volume's free space is below the need plus a safety margin
  (:data:`DEFAULT_SAFETY_MARGIN` = 10%). Escape hatch:
  ``BRUTEX_PREBUILD_DISK_GUARD=off`` (default ON).

Honest envelope (Golden Rule #10):

* the measurement walk is **O(files)** over the lake tree (a stat per
  partition file, no Parquet decode) — NOT O(1); it runs once per pipeline
  prebuild, never per read;
* the multiplier is an OBSERVATION of the lake's CURRENT materialisation state
  — a partially-materialised lake under-observes the fully-materialised cost,
  which is exactly why the guard falls back to the CONSERVATIVE 6.5× when no
  observation exists and why the observation is re-persisted after every
  prebuild run (the ratchet converges on reality);
* the guard FAILS OPEN: a measurement/stat error returns ``None`` (logged
  ``pipeline.prebuild.disk_guard_error``) and the prebuild proceeds — a guard
  bug must never block the pipeline's data stage; the real disk-full backstop
  stays :class:`~brutex.data.errors.LakeWriteHalt` (DEC-LAKE-WRITE-HALT-001).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Mapping

_LOG = structlog.get_logger("brutex.data.lake_footprint")

#: ``schema_meta`` key namespace (mirrors ``fno_candle_lake.*`` — one flat
#: key-value store, namespaced so nothing collides; DEC-FNO-CANDLE-LAKE-001).
META_PREFIX: Final[str] = "lake_footprint."
#: The overall observed multiplier key (``total ÷ source`` as a Decimal string).
META_MULTIPLIER_OVERALL: Final[str] = META_PREFIX + "multiplier_overall"
#: Per-segment observed multiplier keys (empty-source segments persist no key).
META_MULTIPLIER_SPOT: Final[str] = META_PREFIX + "multiplier_spot"
META_MULTIPLIER_FNO: Final[str] = META_PREFIX + "multiplier_fno"
#: The audit sample: the byte counts the multiplier was derived from + when.
META_SOURCE_BYTES: Final[str] = META_PREFIX + "source_1m_bytes"
META_DERIVED_BYTES: Final[str] = META_PREFIX + "derived_bytes"
META_MEASURED_AT: Final[str] = META_PREFIX + "measured_at"

#: The CONSERVATIVE fallback multiplier used when NO observation exists yet
#: (DEC-LAKE-FOOTPRINT-MEASURED-001): total-lake ÷ 1-minute-footprint. Chosen
#: ABOVE the realistic ~5-6× all-in estimate (19 intraday TFs + derived 1day +
#: the 17-column Greeks-enriched option 1-minute rewrite the old "~2-3×" claim
#: ignored) so a first-ever prebuild on a nearly-full volume skips rather than
#: half-writes. Superseded by the measured value after the first prebuild run.
FALLBACK_MULTIPLIER: Final[Decimal] = Decimal("6.5")

#: Safety margin applied ON TOP of the estimated need before comparing against
#: free space (a 10% headroom — Parquet compression variance + concurrent
#: writers). Stated, not hidden (Golden Rule #10).
DEFAULT_SAFETY_MARGIN: Final[Decimal] = Decimal("0.10")

#: The operator escape hatch: ``off`` / ``0`` / ``false`` / ``no`` (any case)
#: DISABLES the pre-run disk guard entirely (default ON).
DISK_GUARD_ENV: Final[str] = "BRUTEX_PREBUILD_DISK_GUARD"
_DISABLED_TOKENS: Final[frozenset[str]] = frozenset({"off", "0", "false", "no"})

#: The fetched base interval — every other stored interval is DERIVED
#: (DEC-1MINUTE-ONLY-DERIVE-1DAY-001: 1-minute is the ONLY vendor-fetched
#: granularity; the 19 intraday TFs + ``1day`` are resampled from it).
_SOURCE_INTERVAL: Final[str] = "1minute"

#: The F&O lake segment token (matches
#: :data:`brutex.data.fno_candle_lake.FNO_CANDLE_LAKE_SEGMENT` — kept as a
#: literal so this module imports nothing heavy).
_FNO_SEGMENT: Final[str] = "FNO"

#: Minimum relative-path depth of a real partition file
#: (``<exchange>/<segment>/<symbol>/<interval>/…/<mm>.parquet`` — anything
#: shallower carries no interval component and is not a partition file).
_MIN_PARTITION_DEPTH: Final[int] = 5

#: Decimal quantum for persisted/derived multipliers (4 dp — a ratio display,
#: NOT money; DEC-MONEY-2DP-001 governs rupee display only).
_MULTIPLIER_QUANTUM: Final[Decimal] = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class SegmentFootprint:
    """One segment's measured byte split: source 1-minute vs derived intervals.

    Attributes
    ----------
    source_1m_bytes
        Total on-disk bytes of the segment's ``1minute`` partitions — including
        any Greeks-enriched 17-column rewrite (the on-disk truth, so the
        enrichment growth is inside the denominator it inflates).
    derived_bytes
        Total on-disk bytes of every OTHER stored interval (the 19 derived
        intraday TFs + the derived ``1day``).
    """

    source_1m_bytes: int
    derived_bytes: int

    @property
    def total_bytes(self) -> int:
        """The segment's whole on-disk footprint (source + derived)."""
        return self.source_1m_bytes + self.derived_bytes

    @property
    def multiplier(self) -> Decimal | None:
        """OBSERVED ``total ÷ source`` multiplier (4 dp), or ``None`` with no source.

        ``None`` (a segment with zero stored 1-minute bytes) is honestly
        distinct from any numeric ratio — never a fabricated ``0`` or ``1``
        (Golden Rule #1).
        """
        if self.source_1m_bytes <= 0:
            return None
        ratio = Decimal(self.total_bytes) / Decimal(self.source_1m_bytes)
        return ratio.quantize(_MULTIPLIER_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class LakeFootprint:
    """The whole lake's measured footprint, split SPOT-side vs FNO.

    ``spot`` covers every non-``FNO`` segment directory (``INDEX`` / ``CASH`` /
    any spot-side segment token); ``fno`` is the F&O candle lake
    (``bars/<exch>/FNO/…`` — the CLAUDE.md §6 F&O-candle-lake layout row).
    """

    spot: SegmentFootprint
    fno: SegmentFootprint

    @property
    def overall(self) -> SegmentFootprint:
        """The lake-wide byte split (spot + fno summed)."""
        return SegmentFootprint(
            source_1m_bytes=self.spot.source_1m_bytes + self.fno.source_1m_bytes,
            derived_bytes=self.spot.derived_bytes + self.fno.derived_bytes,
        )


def measure_lake_footprint(lake_root: Path) -> LakeFootprint:
    """Measure the lake's source-vs-derived byte split with ONE bounded stat-walk.

    Walks ``<lake_root>/bars/<exchange>/<segment>/<symbol>/<interval>/…`` and
    sums each ``*.parquet`` file's size into its segment's SOURCE
    (``interval == "1minute"``) or DERIVED bucket. A file that vanishes
    mid-walk (a concurrent writer's atomic replace) is skipped silently —
    the measurement is a best-effort census, never a lock-holder. A path too
    shallow to carry an interval component is ignored (not a partition file).

    O(files) — a stat per partition file, no Parquet decode (honest envelope;
    the same census cost class as the lake invariant scanner,
    DEC-LAKE-HEAL-001).
    """
    spot_source = spot_derived = fno_source = fno_derived = 0
    bars_root = lake_root / "bars"
    if bars_root.is_dir():
        for file in bars_root.rglob("*.parquet"):
            parts = file.relative_to(bars_root).parts
            # Layout: <exchange>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet
            # (≥ 5 components ⇒ parts[3] is the interval; anything shallower is
            # not a partition file and is skipped).
            if len(parts) < _MIN_PARTITION_DEPTH:
                continue
            segment, interval = parts[1], parts[3]
            try:
                size = file.stat().st_size
            except OSError:
                continue  # vanished mid-walk (concurrent atomic replace) — census skips it
            if segment == _FNO_SEGMENT:
                if interval == _SOURCE_INTERVAL:
                    fno_source += size
                else:
                    fno_derived += size
            elif interval == _SOURCE_INTERVAL:
                spot_source += size
            else:
                spot_derived += size
    return LakeFootprint(
        spot=SegmentFootprint(source_1m_bytes=spot_source, derived_bytes=spot_derived),
        fno=SegmentFootprint(source_1m_bytes=fno_source, derived_bytes=fno_derived),
    )


def persist_footprint(
    set_meta: Callable[[str, str], None],
    footprint: LakeFootprint,
    *,
    measured_at: datetime | None = None,
) -> None:
    """Persist the observed multiplier(s) + audit sample to ``schema_meta``.

    *set_meta* is the store's key-value writer
    (:meth:`brutex.data.instrument_store.InstrumentStore.set_meta` — the SAME
    generic sidecar the master-integrity + economics audits persist through,
    DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001 #875). A segment with no source
    bytes persists NO multiplier key for that segment (never a fabricated
    ratio); the overall multiplier is skipped the same way on an empty lake.
    Idempotent — ``INSERT OR REPLACE`` semantics live in the store.
    """
    stamp = (measured_at or datetime.now(UTC)).isoformat()
    overall = footprint.overall
    for key, multiplier in (
        (META_MULTIPLIER_OVERALL, overall.multiplier),
        (META_MULTIPLIER_SPOT, footprint.spot.multiplier),
        (META_MULTIPLIER_FNO, footprint.fno.multiplier),
    ):
        if multiplier is not None:
            set_meta(key, str(multiplier))
    set_meta(META_SOURCE_BYTES, str(overall.source_1m_bytes))
    set_meta(META_DERIVED_BYTES, str(overall.derived_bytes))
    set_meta(META_MEASURED_AT, stamp)


def load_observed_multiplier(get_meta: Callable[[str], str | None]) -> Decimal | None:
    """Read the last persisted OVERALL multiplier, or ``None`` when absent/invalid.

    A malformed stored value (a corrupted sidecar) reads as ``None`` — the
    guard then uses the conservative :data:`FALLBACK_MULTIPLIER`, never a
    crash and never a garbage estimate (Golden Rule #1).
    """
    raw = get_meta(META_MULTIPLIER_OVERALL)
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        _LOG.warning("lake.footprint.multiplier_invalid", value=raw)
        return None
    if value <= 0:
        _LOG.warning("lake.footprint.multiplier_invalid", value=raw)
        return None
    return value


def record_lake_footprint(
    lake_root: Path,
    set_meta: Callable[[str, str], None],
    *,
    measured_at: datetime | None = None,
) -> LakeFootprint | None:
    """Measure + persist the lake footprint, best-effort (never raises).

    The post-prebuild observation seam: called after the pipeline's prebuild
    phase ran, so the persisted multiplier reflects the freshly-materialised
    state — the value the NEXT pull's disk guard reads. Any error is logged
    (``lake.footprint.record_failed``) and swallowed — the observation can
    never fail the already-successful pull. Returns the measurement, or
    ``None`` on failure.
    """
    try:
        footprint = measure_lake_footprint(lake_root)
        persist_footprint(set_meta, footprint, measured_at=measured_at)
    except Exception:  # pylint: disable=broad-exception-caught — best-effort observation
        _LOG.exception("lake.footprint.record_failed")
        return None
    overall = footprint.overall
    _LOG.info(
        "lake.footprint.measured",
        source_1m_bytes=overall.source_1m_bytes,
        derived_bytes=overall.derived_bytes,
        multiplier=None if overall.multiplier is None else str(overall.multiplier),
        multiplier_spot=None
        if footprint.spot.multiplier is None
        else str(footprint.spot.multiplier),
        multiplier_fno=None if footprint.fno.multiplier is None else str(footprint.fno.multiplier),
    )
    return footprint


def disk_guard_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the pre-run disk guard is enabled (default ON).

    ``BRUTEX_PREBUILD_DISK_GUARD`` ∈ {``off``, ``0``, ``false``, ``no``}
    (any case) disables it — the operator override for a deliberate
    tight-disk run. Any other value (including unset) keeps the guard on.
    """
    value = (env if env is not None else os.environ).get(DISK_GUARD_ENV, "")
    return value.strip().lower() not in _DISABLED_TOKENS


@dataclass(frozen=True, slots=True)
class DiskGuardDecision:
    """The pre-run disk guard's verdict with the ACTUAL numbers it derived.

    Attributes
    ----------
    blocked
        ``True`` ⇒ the prebuild must be SKIPPED (free space below the estimated
        need + margin). ``False`` ⇒ proceed.
    free_bytes
        The lake volume's free bytes at evaluation time.
    estimated_need_bytes
        The estimated ADDITIONAL bytes the prebuild would write
        (``max(0, source × multiplier − (source + derived already on disk))``
        — bytes already on disk are sunk cost, not future need).
    source_1m_bytes, derived_bytes
        The lake-wide measured split feeding the estimate.
    multiplier
        The multiplier used (observed, or the 6.5× fallback).
    multiplier_source
        ``"observed"`` (from ``schema_meta``) or ``"fallback"`` (no observation
        yet) — so the log/headline is honest about the estimate's provenance.
    margin
        The safety margin applied (fraction, e.g. ``0.10``).
    """

    blocked: bool
    free_bytes: int
    estimated_need_bytes: int
    source_1m_bytes: int
    derived_bytes: int
    multiplier: Decimal
    multiplier_source: str
    margin: Decimal

    @property
    def detail(self) -> str:
        """One honest human-readable clause for the pipeline headline/log."""
        need_gib = self.estimated_need_bytes / 2**30
        free_gib = self.free_bytes / 2**30
        return (
            f"disk guard: est. need ~{need_gib:.1f} GiB "
            f"(x{self.multiplier} {self.multiplier_source} multiplier, "
            f"+{int(self.margin * 100)}% margin), free {free_gib:.1f} GiB — "
            f"prebuild skipped; free space or set {DISK_GUARD_ENV}=off to override"
        )


def volume_free_bytes(root: Path) -> int:
    """Free bytes on the volume that holds (or will hold) *root*.

    The nearest-existing-parent probe EXTRACTED verbatim from
    :func:`evaluate_disk_guard` (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 — the
    sweep's fail-CLOSED results-volume disk gate reuses it instead of copying
    it; CLAUDE.md §11 COMMON): a not-yet-created *root* (a first-ever lake /
    results dir) walks up to its nearest EXISTING ancestor — the volume the
    directory would be created on — and reads ``shutil.disk_usage(...).free``
    there. O(path-depth) stats + one ``statvfs``; never creates anything.

    Parameters
    ----------
    root
        The directory whose volume to probe (need not exist yet).

    Returns
    -------
    int
        Free bytes on the probed volume.

    Raises
    ------
    OSError
        If the ``disk_usage`` probe itself fails (an unreadable / vanished
        mount) — the CALLER decides the policy (the prebuild guard fails
        OPEN via :func:`make_disk_guard`; the sweep's ``check_results_disk``
        maps it to a fail-CLOSED refusal).
    """
    probe = root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def evaluate_disk_guard(
    lake_root: Path,
    *,
    observed_multiplier: Decimal | None,
    free_bytes: int | None = None,
    margin: Decimal = DEFAULT_SAFETY_MARGIN,
) -> DiskGuardDecision:
    """Derive the pre-run disk-guard verdict from the measured lake state.

    ``estimated_total = source_1m_bytes × multiplier`` (the observed
    ``schema_meta`` value when present, else the conservative
    :data:`FALLBACK_MULTIPLIER`); the NEED subtracts everything already on
    disk (source + derived — sunk cost); the verdict blocks when
    ``free < need × (1 + margin)`` and the need is positive. *free_bytes* is
    injectable for tests; production probes the lake volume via the public
    :func:`volume_free_bytes` (byte-identical delegation of the former inline
    nearest-existing-parent walk).
    """
    footprint = measure_lake_footprint(lake_root)
    overall = footprint.overall
    multiplier = observed_multiplier if observed_multiplier is not None else FALLBACK_MULTIPLIER
    source_label = "observed" if observed_multiplier is not None else "fallback"
    estimated_total = int(Decimal(overall.source_1m_bytes) * multiplier)
    need = max(0, estimated_total - overall.total_bytes)
    if free_bytes is None:
        free_bytes = volume_free_bytes(lake_root)
    blocked = need > 0 and free_bytes < int(Decimal(need) * (Decimal(1) + margin))
    return DiskGuardDecision(
        blocked=blocked,
        free_bytes=free_bytes,
        estimated_need_bytes=need,
        source_1m_bytes=overall.source_1m_bytes,
        derived_bytes=overall.derived_bytes,
        multiplier=multiplier,
        multiplier_source=source_label,
        margin=margin,
    )


def make_disk_guard(
    lake_root: Path,
    get_meta: Callable[[str], str | None] | None,
    *,
    env: Mapping[str, str] | None = None,
) -> Callable[[], DiskGuardDecision | None]:
    """Build the guarded zero-arg disk-guard callable the prebuild phase calls.

    Returns a closure that (a) returns ``None`` immediately when the guard is
    DISABLED (``BRUTEX_PREBUILD_DISK_GUARD=off``) or when *get_meta* is
    ``None`` (no instrument store wired — e.g. a bare test harness: the
    fallback multiplier still applies via the ``None`` observed value, so a
    store-less binding keeps the guard ONLY when a lake exists to measure);
    (b) otherwise evaluates :func:`evaluate_disk_guard` with the last observed
    multiplier. FAILS OPEN: any evaluation error logs
    ``pipeline.prebuild.disk_guard_error`` and returns ``None`` — a guard bug
    can never block the pipeline's data stage (the real disk-full backstop is
    :class:`~brutex.data.errors.LakeWriteHalt`).
    """

    def _guard() -> DiskGuardDecision | None:
        if not disk_guard_enabled(env):
            return None
        try:
            observed = load_observed_multiplier(get_meta) if get_meta is not None else None
            return evaluate_disk_guard(lake_root, observed_multiplier=observed)
        except Exception:  # pylint: disable=broad-exception-caught — the guard fails OPEN
            _LOG.exception("pipeline.prebuild.disk_guard_error")
            return None

    return _guard


__all__ = [
    "DEFAULT_SAFETY_MARGIN",
    "DISK_GUARD_ENV",
    "FALLBACK_MULTIPLIER",
    "META_DERIVED_BYTES",
    "META_MEASURED_AT",
    "META_MULTIPLIER_FNO",
    "META_MULTIPLIER_OVERALL",
    "META_MULTIPLIER_SPOT",
    "META_PREFIX",
    "META_SOURCE_BYTES",
    "DiskGuardDecision",
    "LakeFootprint",
    "SegmentFootprint",
    "disk_guard_enabled",
    "evaluate_disk_guard",
    "load_observed_multiplier",
    "make_disk_guard",
    "measure_lake_footprint",
    "persist_footprint",
    "record_lake_footprint",
    "volume_free_bytes",
]
