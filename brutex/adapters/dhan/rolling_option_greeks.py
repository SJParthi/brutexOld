"""Rolling-ATM option GREEKS enrichment — the Dhan track's §3 pass (no math copy).

DEC-DHAN-ROLLING-OPTIONS-001 §3. Enriches the Dhan **rolling-ATM** 1-minute
option bars with the nullable Greeks overlay reserved by
:mod:`brutex.adapters.dhan.rolling_option_writer`, REUSING the COMMON Greeks
math verbatim — the SAME per-bar pricer
(:func:`brutex.data.greeks_enrich.compute_bar_overlay`, the public alias of
``_compute_bar``) → :func:`brutex.options.greeks.compute_greeks` → the shared
``trading_core`` Rust kernel. NO Greek arithmetic is re-implemented here; this
module is I/O + per-row decode + the reuse/idempotency gate + the fence.

The ONE genuine new cross-cutting seam is ``iv_source`` (served-vs-solved),
which lives in the COMMON pricer + provenance (byte-identical for Groww). Dhan
SERVES an implied volatility per bar, so when it is usable
(:func:`~brutex.data.greeks_enrich.served_iv_usable`) the Greeks are priced
DIRECTLY from ``(resolved_strike, spot, served_iv, T, rate)`` with NO root-solve
(``iv_source="served"``); when it is absent/garbage the pricer solves from the
premium exactly as Groww does (``iv_source="solved"``), and an unsolvable
premium stamps ``no_solution`` — never a fabricated Greek.

Rolling-ATM specifics (the honesty crux — DEC-DHAN-ROLLING-OPTIONS-001):

* **Per-ROW decode, NO ``parse_fno_symbol``.** ``strike`` = the row's
  ``resolved_strike``, ``is_call`` = ``option_type == "CALL"``, and the expiry
  instant is built from THAT row's ``resolved_expiry_date`` — the "contract"
  drifts with the ATM selector, so ``T`` binds per bar, never per partition. A
  row whose ``resolved_expiry_source == "unresolved"`` (no date) is an honest
  UNDECODABLE skip (counted, its overlay left null — never a fabricated expiry).
* **Spot is per-ROW.** The underlying spot is the row's served ``spot`` column
  (all three underlyings are indices → :data:`STYLE_EUROPEAN_EXACT`), so there
  is NO separate spot lake scan and NO spot-join memo — the row carries its own
  spot (absent → the honest ``no_spot`` NULL).
* **Rolling scheme only.** ``read_bars`` walks the ``rolling_options/`` tree,
  structurally disjoint from every fixed-contract ``bars/<exch>/FNO/`` reader.

Scope / limits (Golden Rule #10): runs on the **1-minute** rolling-option bars
ONLY (index spot has no Greeks; a derived-TF Greek would need a coarse-bucket
IV Dhan never serves — a documented limit). The driver here is SEQUENTIAL — the
spawn-pool parallelism of the Groww driver is a deferred follow-up (an injected
seam forces the Groww driver sequential too); at 1-minute option-bar volumes the
kernel solve dominates, so this is a correctness pass, never a throughput claim.
Not wired into a ROUTE this pass (the CLI ``enrich-greeks --feed dhan`` arm wires
:func:`run_dhan_rolling_greeks_pass`); the dashboard route surface is a later step (§5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.adapters.dhan.rolling_option_constants import (
    DHAN_ROLLING_FETCH_INTERVAL,
    DHAN_ROLLING_SUBDIR,
    OPTION_TYPE_CALL,
)
from brutex.adapters.dhan.rolling_option_parser import RESOLVED_EXPIRY_SOURCE_UNRESOLVED
from brutex.data.atomic_io import atomic_write_via, lake_write_guard
from brutex.data.errors import (
    GreeksEnrichPoolBroken,
    LakeWriteHalt,
    parquet_read_skip_errors,
)
from brutex.data.greeks_enrich import (
    compute_bar_overlay,
    current_inputs_pack,
    expiry_instant_utc,
    rate_source_id_for,
    resolve_risk_free_source,
    served_iv_usable,
    t_years_act365,
)
from brutex.data.greeks_enrich_pass import GREEKS_NOT_RUN, GreeksEnrichOutcome
from brutex.data.greeks_provenance import (
    INPUTS_MASK,
    IV_SOURCE_NAMES,
    IV_SOURCE_SERVED,
    IV_SOURCE_SOLVED,
    STATUS_NAMES,
    STATUS_OK,
    STYLE_EUROPEAN_EXACT,
    unpack_greeks_provenance,
)
from brutex.data.greeks_revision import touch_greeks_revision_marker
from brutex.data.models import FnoGreeksBar
from brutex.data.partition import partition_for_timestamp
from brutex.data.partition_lock import BoundedLockRegistry, partition_write_lock
from brutex.money import round_money
from brutex.observability.progress_beat import emit_progress_beat

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import polars as pl
    from structlog.typing import FilteringBoundLogger

    from brutex.options.risk_free import RiskFreeRate, RiskFreeSource

_LOG = structlog.get_logger("brutex.adapters.dhan.rolling_option_greeks")

#: The nullable Greeks-overlay columns the rolling writer reserves, in stored
#: order — the SAME names :mod:`~brutex.adapters.dhan.rolling_option_writer`
#: writes (``iv_used`` — NOT ``iv``, which is the vendor-SERVED input column —
#: plus the six Greeks, the snapped spot, the two inputs, the packed stamp, and
#: the Utf8 ``iv_source`` mirror of the provenance bit).
_OVERLAY_FLOAT_COLUMNS: Final[tuple[str, ...]] = (
    "iv_used",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "spot_at_bar",
    "t_years_used",
    "rate_used",
)

#: Every overlay column the write-back replaces (the floats + the Int64 stamp +
#: the Utf8 ``iv_source`` mirror).
_OVERLAY_COLUMNS: Final[tuple[str, ...]] = (
    *_OVERLAY_FLOAT_COLUMNS,
    "greeks_provenance_id",
    "iv_source",
)

#: Physical (non-overlay) row columns the writer stores — read back verbatim,
#: never mutated by the enrichment (the overlay is additive).
_PHYSICAL_COLUMNS: Final[tuple[str, ...]] = (
    "schema_version",
    "exch_id",
    "exchange_segment",
    "security_id",
    "underlying_symbol",
    "expiry_flag",
    "expiry_code",
    "strike_offset",
    "option_type",
    "interval",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "iv",
    "spot",
    "resolved_strike",
    "resolved_expiry_date",
    "resolved_expiry_source",
)


class RollingGreeksError(Exception):
    """A rolling-ATM Greeks read/write seam failed for one target (quarantined)."""


@dataclass(frozen=True, slots=True)
class RollingGreeksTarget:
    """One rolling-ATM option partition key to enrich (the six selectors + interval).

    The Dhan analogue of :class:`~brutex.data.greeks_enrich.GreeksEnrichTarget`,
    but keyed by the REQUESTED rolling selectors (NEVER a resolved
    date/strike — those drift per row). ``strike_offset_safe`` is the
    path-hazard-safe segment as stored on disk (``ATMp1``), so the partition
    directory rebuilds byte-identically to the writer's.

    Attributes
    ----------
    exch_id
        The exchange tree segment (``NSE`` / ``BSE``).
    underlying
        The underlying symbol segment (``NIFTY`` / ``BANKNIFTY`` / ``SENSEX``).
    expiry_flag
        ``WEEK`` / ``MONTH``.
    expiry_code
        The relative expiry code (``0..3``).
    strike_offset_safe
        The path-safe ATM-relative segment (``ATM`` / ``ATMp1`` / ``ATMm10``).
    option_type
        ``CALL`` / ``PUT``.
    interval
        Always ``1minute`` (the only granularity carrying a Greeks overlay).
    """

    exch_id: str
    underlying: str
    expiry_flag: str
    expiry_code: int
    strike_offset_safe: str
    option_type: str
    interval: str

    def partition_dir(self, lake_root: Path) -> Path:
        """Rebuild this key's on-disk partition directory under *lake_root*."""
        return (
            lake_root
            / DHAN_ROLLING_SUBDIR
            / self.exch_id
            / self.underlying
            / self.expiry_flag
            / f"EC{self.expiry_code}"
            / self.strike_offset_safe
            / self.option_type
            / self.interval
        )


@dataclass(frozen=True, slots=True)
class RollingGreeksOverlay:
    """The enriched (or restated) Greeks overlay of ONE rolling-ATM bar.

    ``iv_source`` is the human-readable mirror (``"served"`` / ``"solved"``) of
    the packed provenance bit — written to the Utf8 ``iv_source`` column for the
    ``/db`` surface; the AUTHORITATIVE staleness key is the bit inside
    ``greeks_provenance_id``.
    """

    timestamp: datetime
    iv_used: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    rho: float | None
    spot_at_bar: Decimal | None
    t_years_used: float | None
    rate_used: float | None
    greeks_provenance_id: int | None
    iv_source: str | None


@dataclass(frozen=True, slots=True)
class RollingEnrichStats:
    """Per-target tally of one :func:`enrich_rolling_option_bars` pass.

    Attributes
    ----------
    served
        OK bars priced DIRECTLY from the vendor-served IV (``iv_source="served"``).
    solved
        OK bars whose IV was root-solved from the premium (``iv_source="solved"``).
    nulls
        Bars stamped an honest NULL reason (any non-OK status).
    reused
        Bars whose stored stamp already matched the current inputs + regime —
        restated verbatim (idempotent fast path).
    changed
        Bars whose overlay differs from what is stored — ``0`` = the target is
        already overlay-current and the pass issues no write.
    skipped
        Rows dropped by the non-finite OHLC screen (no bar built).
    undecodable
        Rows whose ``resolved_expiry_source`` is ``unresolved`` (no expiry
        date) — counted, overlay left null, never a fabricated expiry.
    null_reasons
        Sorted ``(status_name, count)`` pairs — the per-status NULL breakdown.
    """

    served: int
    solved: int
    nulls: int
    reused: int
    changed: int
    skipped: int
    undecodable: int
    null_reasons: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class RollingGreeksReport:
    """Aggregate outcome of one :func:`enrich_rolling_greeks_for_targets` pass."""

    enriched: int
    already_current: int
    no_bars: int
    errored: int
    bars_written: int
    bars_served: int
    bars_solved: int
    bars_null: int
    bars_reused: int
    bars_skipped: int
    bars_undecodable: int


def _as_float(value: object) -> float | None:
    """A stored nullable Parquet numeric as ``float | None``."""
    return None if value is None else float(value)  # type: ignore[arg-type]


def _as_decimal(value: object) -> Decimal | None:
    """A stored nullable Parquet price as an exact ``Decimal | None``."""
    return None if value is None else Decimal(str(value))


def _stored_overlay(row: Mapping[str, object]) -> RollingGreeksOverlay:
    """Reconstruct the overlay a stored row already carries (normalised)."""
    return RollingGreeksOverlay(
        timestamp=row["timestamp"],  # type: ignore[arg-type]
        iv_used=_as_float(row.get("iv_used")),
        delta=_as_float(row.get("delta")),
        gamma=_as_float(row.get("gamma")),
        theta=_as_float(row.get("theta")),
        vega=_as_float(row.get("vega")),
        rho=_as_float(row.get("rho")),
        spot_at_bar=_as_decimal(row.get("spot_at_bar")),
        t_years_used=_as_float(row.get("t_years_used")),
        rate_used=_as_float(row.get("rate_used")),
        greeks_provenance_id=(
            None if row.get("greeks_provenance_id") is None else int(row["greeks_provenance_id"])  # type: ignore[call-overload]
        ),
        iv_source=(None if row.get("iv_source") is None else str(row["iv_source"])),
    )


def _overlay_of_bar(
    bar: FnoGreeksBar, *, timestamp: datetime, iv_source_name: str
) -> RollingGreeksOverlay:
    """Map a freshly-priced :class:`FnoGreeksBar` onto the rolling overlay columns.

    ``FnoGreeksBar.iv`` (the solved/served IV) maps to the ``iv_used`` overlay
    column — distinct from the vendor-served ``iv`` INPUT column, which the
    enrichment never touches.
    """
    return RollingGreeksOverlay(
        timestamp=timestamp,
        iv_used=bar.iv,
        delta=bar.delta,
        gamma=bar.gamma,
        theta=bar.theta,
        vega=bar.vega,
        rho=bar.rho,
        spot_at_bar=bar.spot_at_bar,
        t_years_used=bar.t_years_used,
        rate_used=bar.rate_used,
        greeks_provenance_id=bar.greeks_provenance_id,
        iv_source=iv_source_name,
    )


def _iv_source_name(provenance_id: int) -> str:
    """The ``"served"`` / ``"solved"`` label packed in a fresh provenance stamp."""
    return IV_SOURCE_NAMES[unpack_greeks_provenance(provenance_id).iv_source]


def enrich_rolling_option_bars(
    rows: Sequence[Mapping[str, object]],
    *,
    rate_source: RiskFreeSource,
    style_flag: int = STYLE_EUROPEAN_EXACT,
) -> tuple[list[RollingGreeksOverlay], RollingEnrichStats]:
    """Compute the Greeks overlay for one rolling-ATM key's bars — pure core.

    Mirrors the ORCHESTRATION of
    :func:`brutex.data.greeks_enrich.enrich_option_bars` (the per-bar reuse gate
    + change tally) but decodes the "contract" PER ROW (the rolling-ATM crux)
    and routes each bar through the SAME shared pricer
    (:func:`~brutex.data.greeks_enrich.compute_bar_overlay`) — passing the row's
    served IV so a usable one takes the direct served-IV branch. No Greek math
    is re-implemented.

    Per row:

    * ``resolved_expiry_source == "unresolved"`` (or a null
      ``resolved_expiry_date``) → UNDECODABLE skip (counted; overlay left null).
    * a non-finite OHLC field → SKIP (no bar built; the raw row is untouched).
    * a stored stamp that matches the current inputs, style, status ``OK``,
      ``rate_used`` / ``spot_at_bar`` VALUES **and** the served-vs-solved regime
      the current served IV implies → REUSE (restate verbatim, no recompute).
    * otherwise recompute through the pricer; ``T`` binds to THIS row's
      ``resolved_expiry_date``, the spot is THIS row's served ``spot``.

    Returns the overlays for every DECODABLE, non-skipped row (reused +
    recomputed, input order) and the change tally. ``changed == 0`` means the
    key is already overlay-current and needs no write.
    """
    source_id = rate_source_id_for(rate_source)
    current_inputs = current_inputs_pack(rate_source)
    rate_by_date: dict[date, RiskFreeRate | None] = {}

    overlays: list[RollingGreeksOverlay] = []
    served = solved = nulls = reused = changed = skipped = undecodable = 0
    reasons: dict[int, int] = {}
    for row in rows:
        ts: datetime = row["timestamp"]  # type: ignore[assignment]
        expiry_source = str(row["resolved_expiry_source"])
        expiry_date = row.get("resolved_expiry_date")
        resolved_strike = row.get("resolved_strike")
        if (
            expiry_source == RESOLVED_EXPIRY_SOURCE_UNRESOLVED
            or expiry_date is None
            or resolved_strike is None
        ):
            # No resolved expiry / strike for this bar-date — never a fabricated
            # contract. Counted, overlay left null (the honest undecodable skip).
            undecodable += 1
            continue
        open_ = Decimal(str(row["open"]))
        high = Decimal(str(row["high"]))
        low = Decimal(str(row["low"]))
        close = Decimal(str(row["close"]))
        if not all(p.is_finite() for p in (open_, high, low, close)):
            skipped += 1
            continue
        volume = int(row["volume"])  # type: ignore[call-overload]
        raw_oi = row.get("open_interest")
        oi = None if raw_oi is None else int(raw_oi)  # type: ignore[call-overload]
        spot = _as_decimal(row.get("spot"))
        if spot is not None and (not spot.is_finite() or spot <= 0):
            spot = None
        served_iv = _as_float(row.get("iv"))
        # The served-vs-solved regime this pass WOULD stamp on an OK bar — used
        # by the reuse gate so a re-pull that fills a previously-absent IV
        # (solved -> served) re-derives instead of restating the stale Greek.
        regime_flag = IV_SOURCE_SERVED if served_iv_usable(served_iv) else IV_SOURCE_SOLVED

        bar_date = (ts + timedelta(hours=5, minutes=30)).date()
        if bar_date not in rate_by_date:
            rate_by_date[bar_date] = rate_source.rate_on(bar_date)
        rate = rate_by_date[bar_date]

        stored_pid = row.get("greeks_provenance_id")
        if isinstance(stored_pid, int):
            stored = unpack_greeks_provenance(stored_pid)
            # CORRECTNESS DEPENDENCY: the reuse gate compares the served-vs-solved
            # REGIME bit (``iv_source``), not the raw served-IV value. This is sound
            # ONLY because the raw rolling writer is KEEP-FIRST (RollingOptionWriter —
            # served_iv/strike/spot are immutable on re-pull). If that writer ever
            # moved to latest-wins, a CORRECTED served IV under unchanged spot/rate
            # would keep the same regime bit and silently reuse a STALE Greek.
            if (
                (stored_pid & INPUTS_MASK) == current_inputs
                and stored.status == STATUS_OK
                and stored.style_flag == style_flag
                and stored.iv_source == regime_flag
                and rate is not None
                and _as_float(row.get("rate_used")) == rate.rate
                and spot is not None
                and _as_decimal(row.get("spot_at_bar")) == round_money(spot)
            ):
                overlays.append(_stored_overlay(row))
                reused += 1
                continue

        t_years = t_years_act365(ts, expiry_instant_utc(expiry_date))  # type: ignore[arg-type]
        bar = compute_bar_overlay(
            ts=ts,
            open_=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            oi=oi,
            spot=spot,
            rate=rate,
            t_years=t_years,
            strike_f=float(Decimal(str(resolved_strike))),
            is_call=str(row["option_type"]) == OPTION_TYPE_CALL,
            style_flag=style_flag,
            source_id=source_id,
            served_iv=served_iv,
        )
        pid = bar.greeks_provenance_id or 0
        status = unpack_greeks_provenance(pid).status
        iv_source_name = _iv_source_name(pid)
        if status == STATUS_OK:
            if iv_source_name == IV_SOURCE_NAMES[IV_SOURCE_SERVED]:
                served += 1
            else:
                solved += 1
        else:
            nulls += 1
            reasons[status] = reasons.get(status, 0) + 1
        overlay = _overlay_of_bar(bar, timestamp=ts, iv_source_name=iv_source_name)
        overlays.append(overlay)
        if overlay != _stored_overlay(row):
            changed += 1
    return overlays, RollingEnrichStats(
        served=served,
        solved=solved,
        nulls=nulls,
        reused=reused,
        changed=changed,
        skipped=skipped,
        undecodable=undecodable,
        null_reasons=tuple(sorted((STATUS_NAMES[s], c) for s, c in reasons.items())),
    )


#: Process-wide per-partition write-lock registry (the writer's idiom, reused).
_PARTITION_LOCKS: BoundedLockRegistry = BoundedLockRegistry()


def discover_rolling_option_targets(lake_root: Path) -> tuple[RollingGreeksTarget, ...]:
    """Discover every rolling-ATM 1-minute key with stored bars under *lake_root*.

    One bounded directory walk of
    ``rolling_options/{exch}/{underlying}/{flag}/EC{code}/{offset}/{type}/1minute/``
    — zero Parquet opens (honest cost: O(rolling keys), NOT O(1)). Only keys
    with at least one ``<yyyy>/<mm>.parquet`` month file are emitted; a
    malformed ``EC{code}`` segment is skipped silently (the discovery-tolerance
    convention). Deterministically sorted so the pass order is stable.
    """
    root = lake_root / DHAN_ROLLING_SUBDIR
    if not root.is_dir():
        return ()
    targets: list[RollingGreeksTarget] = []
    for exch_dir in sorted(root.iterdir()):
        if not exch_dir.is_dir():
            continue
        for under_dir in sorted(exch_dir.iterdir()):
            if not under_dir.is_dir():
                continue
            for flag_dir in sorted(under_dir.iterdir()):
                if not flag_dir.is_dir():
                    continue
                for ec_dir in sorted(flag_dir.iterdir()):
                    code = _expiry_code_of(ec_dir)
                    if code is None:
                        continue
                    for off_dir in sorted(ec_dir.iterdir()):
                        if not off_dir.is_dir():
                            continue
                        for type_dir in sorted(off_dir.iterdir()):
                            if not type_dir.is_dir():
                                continue
                            interval_dir = type_dir / DHAN_ROLLING_FETCH_INTERVAL
                            if not _has_month_file(interval_dir):
                                continue
                            targets.append(
                                RollingGreeksTarget(
                                    exch_id=exch_dir.name,
                                    underlying=under_dir.name,
                                    expiry_flag=flag_dir.name,
                                    expiry_code=code,
                                    strike_offset_safe=off_dir.name,
                                    option_type=type_dir.name,
                                    interval=DHAN_ROLLING_FETCH_INTERVAL,
                                )
                            )
    return tuple(targets)


def _expiry_code_of(ec_dir: Path) -> int | None:
    """Parse the ``EC{n}`` segment into its integer code (junk → ``None``)."""
    name = ec_dir.name
    if not ec_dir.is_dir() or not name.startswith("EC"):
        return None
    stem = name[2:]
    if not (stem.isascii() and stem.isdigit()):
        return None
    return int(stem)


def _has_month_file(interval_dir: Path) -> bool:
    """``True`` iff the interval dir holds at least one ``<yyyy>/<mm>.parquet``."""
    if not interval_dir.is_dir():
        return False
    for year_dir in interval_dir.iterdir():
        if not year_dir.is_dir():
            continue
        for month_file in year_dir.iterdir():
            if month_file.name.endswith(".parquet"):
                return True
    return False


def read_rolling_option_rows(
    lake_root: Path, target: RollingGreeksTarget
) -> list[dict[str, object]]:
    """Read one rolling-ATM key's stored 1-minute rows (schema-tolerant, dedup'd).

    The default ``read_bars`` seam: concatenates the key's month files, fills a
    missing overlay column with a typed null (a pre-Greeks partition), keeps the
    FIRST row per ``timestamp`` (the writer's keep-first identity), and returns
    ascending-time named rows. A missing partition yields ``[]``.
    """
    import polars as pl

    interval_dir = target.partition_dir(lake_root)
    if not interval_dir.is_dir():
        return []
    frames: list[pl.DataFrame] = []
    for year_dir in sorted(interval_dir.iterdir()):
        if not year_dir.is_dir():
            continue
        for month_file in sorted(year_dir.iterdir()):
            if not month_file.name.endswith(".parquet"):
                continue
            try:
                df = pl.read_parquet(month_file, glob=False)
            except parquet_read_skip_errors() as exc:
                raise RollingGreeksError(
                    f"corrupt rolling-option partition {month_file}: {exc}"
                ) from exc
            df = _fill_missing_overlay(df)
            frames.append(df.select(*_PHYSICAL_COLUMNS, *_OVERLAY_COLUMNS))
    if not frames:
        return []
    merged = (
        pl.concat(frames)
        .unique(subset=["timestamp"], keep="first", maintain_order=True)
        .sort("timestamp")
    )
    return list(merged.iter_rows(named=True))


def _fill_missing_overlay(frame: pl.DataFrame) -> pl.DataFrame:
    """Add typed null overlay columns absent from a pre-Greeks partition."""
    import polars as pl

    additions = []
    for name in _OVERLAY_FLOAT_COLUMNS:
        if name not in frame.columns:
            additions.append(pl.lit(None, dtype=pl.Float64).alias(name))
    if "greeks_provenance_id" not in frame.columns:
        additions.append(pl.lit(None, dtype=pl.Int64).alias("greeks_provenance_id"))
    if "iv_source" not in frame.columns:
        additions.append(pl.lit(None, dtype=pl.Utf8).alias("iv_source"))
    if additions:
        frame = frame.with_columns(*additions)
    return frame


def write_rolling_option_greeks(
    lake_root: Path,
    target: RollingGreeksTarget,
    overlays: Sequence[RollingGreeksOverlay],
) -> int:
    """Merge enriched overlays into the key's partitions latest-wins by timestamp.

    The default ``write_bars`` seam: per month partition, the overlay columns of
    every row whose ``timestamp`` appears in *overlays* are OVERWRITTEN (a
    corrected/re-derived overlay wins; an identical restate is a byte-identical
    no-op); the raw physical columns are untouched. Runs under the per-path +
    cross-process partition locks + :func:`lake_write_guard` (ENOSPC / OOM →
    :class:`~brutex.data.errors.LakeWriteHalt`). Returns the count of stored bars
    whose overlay was replaced.
    """
    import polars as pl

    by_month: dict[tuple[int, int], dict[datetime, RollingGreeksOverlay]] = {}
    for overlay in overlays:
        by_month.setdefault(partition_for_timestamp(overlay.timestamp), {})[overlay.timestamp] = (
            overlay
        )
    interval_dir = target.partition_dir(lake_root)
    written = 0
    for (year, month), month_overlays in sorted(by_month.items()):
        dest = interval_dir / f"{year:04d}" / f"{month:02d}.parquet"
        if not dest.exists():
            continue
        with (
            _PARTITION_LOCKS.acquire(dest),
            partition_write_lock(lake_root, dest),
            lake_write_guard(),
        ):
            try:
                existing = pl.read_parquet(dest, glob=False)
            except parquet_read_skip_errors() as exc:
                raise RollingGreeksError(f"corrupt rolling-option partition {dest}: {exc}") from exc
            existing = _fill_missing_overlay(existing)
            update = _overlay_update_frame(month_overlays)
            combined = existing.drop(_OVERLAY_COLUMNS).join(update, on="timestamp", how="left")
            atomic_write_via(dest, combined.select(existing.columns).write_parquet)
            written += len(month_overlays)
    return written


def _overlay_update_frame(
    month_overlays: Mapping[datetime, RollingGreeksOverlay],
) -> pl.DataFrame:
    """Build the typed ``timestamp`` → overlay-columns update frame for one month."""
    import polars as pl

    items = sorted(month_overlays.values(), key=lambda o: o.timestamp)
    return pl.DataFrame(
        {
            "timestamp": [o.timestamp for o in items],
            "iv_used": [o.iv_used for o in items],
            "delta": [o.delta for o in items],
            "gamma": [o.gamma for o in items],
            "theta": [o.theta for o in items],
            "vega": [o.vega for o in items],
            "rho": [o.rho for o in items],
            "spot_at_bar": [None if o.spot_at_bar is None else float(o.spot_at_bar) for o in items],
            "t_years_used": [o.t_years_used for o in items],
            "rate_used": [o.rate_used for o in items],
            "greeks_provenance_id": [o.greeks_provenance_id for o in items],
            "iv_source": [o.iv_source for o in items],
        }
    ).with_columns(
        *(pl.col(name).cast(pl.Float64) for name in _OVERLAY_FLOAT_COLUMNS),
        pl.col("greeks_provenance_id").cast(pl.Int64),
        pl.col("iv_source").cast(pl.Utf8),
    )


#: Injectable read seam type — one key's stored rows.
type RollingBarReader = Callable[[RollingGreeksTarget], list[dict[str, object]]]

#: Injectable write seam type — merge enriched overlays; return replaced count.
type RollingGreeksWriter = Callable[[RollingGreeksTarget, "Sequence[RollingGreeksOverlay]"], int]


def enrich_rolling_greeks_for_targets(
    *,
    targets: Sequence[RollingGreeksTarget],
    lake_root: Path,
    rate_source: RiskFreeSource,
    read_bars: RollingBarReader | None = None,
    write_bars: RollingGreeksWriter | None = None,
) -> RollingGreeksReport:
    """Enrich every rolling-ATM key's stored bars with Greeks — the pass driver.

    SEQUENTIAL (parallelism is a deferred follow-up — an injected seam forces the
    Groww driver sequential too, and the kernel solve dominates at 1-minute
    volumes). Each target is QUARANTINED (a per-key read/enrich/write error
    increments ``errored`` and continues — one bad partition never sinks the
    pass); a :class:`~brutex.data.errors.LakeWriteHalt` (disk full / OOM)
    re-raises past the pass. An ``already_current`` key (nothing changed) is
    skipped with NO write, so an immediate re-run is a byte-identical no-op
    (Golden Rule #7). Lake I/O is injectable for tests; the defaults are the
    real rolling-scheme reader/writer.
    """
    reader = read_bars if read_bars is not None else _bind_reader(lake_root)
    writer = write_bars if write_bars is not None else _bind_writer(lake_root)
    counters = _MutableReport()
    for target in targets:
        emit_progress_beat()
        try:
            rows = reader(target)
            if not rows:
                counters.no_bars += 1
                continue
            overlays, stats = enrich_rolling_option_bars(rows, rate_source=rate_source)
            counters.bars_skipped += stats.skipped
            counters.bars_undecodable += stats.undecodable
            if stats.changed == 0:
                counters.already_current += 1
                _emit_key_event(target, stats, written=0)
                continue
            written = writer(target, overlays)
            counters.enriched += 1
            counters.bars_written += written
            counters.bars_served += stats.served
            counters.bars_solved += stats.solved
            counters.bars_null += stats.nulls
            counters.bars_reused += stats.reused
            _emit_key_event(target, stats, written=written)
        except LakeWriteHalt:
            raise
        except Exception as exc:
            counters.errored += 1
            _LOG.warning(
                "dhan.rolling_greeks.key_errored",
                key=_key_label(target),
                error=str(exc),
            )
    return counters.report()


def _bind_reader(lake_root: Path) -> RollingBarReader:
    """Bind the default rolling-scheme reader over *lake_root*."""
    return lambda t: read_rolling_option_rows(lake_root, t)


def _bind_writer(lake_root: Path) -> RollingGreeksWriter:
    """Bind the default latest-wins overlay writer over *lake_root*."""
    return lambda t, o: write_rolling_option_greeks(lake_root, t, o)


def _key_label(target: RollingGreeksTarget) -> str:
    """A stable human label for one rolling key (log/event field)."""
    return (
        f"{target.exch_id}/{target.underlying}/{target.expiry_flag}/"
        f"EC{target.expiry_code}/{target.strike_offset_safe}/{target.option_type}"
    )


def _emit_key_event(
    target: RollingGreeksTarget, stats: RollingEnrichStats, *, written: int
) -> None:
    """Emit the per-key enrichment structlog event."""
    _LOG.info(
        "dhan.rolling_greeks.key_enriched",
        key=_key_label(target),
        written=written,
        served=stats.served,
        solved=stats.solved,
        nulls=stats.nulls,
        reused=stats.reused,
        undecodable=stats.undecodable,
        null_reasons=dict(stats.null_reasons),
    )


@dataclass(slots=True)
class _MutableReport:
    """Mutable fold accumulator — one per pass."""

    enriched: int = 0
    already_current: int = 0
    no_bars: int = 0
    errored: int = 0
    bars_written: int = 0
    bars_served: int = 0
    bars_solved: int = 0
    bars_null: int = 0
    bars_reused: int = 0
    bars_skipped: int = 0
    bars_undecodable: int = 0

    def report(self) -> RollingGreeksReport:
        """Freeze the folded tallies into the immutable report."""
        return RollingGreeksReport(
            enriched=self.enriched,
            already_current=self.already_current,
            no_bars=self.no_bars,
            errored=self.errored,
            bars_written=self.bars_written,
            bars_served=self.bars_served,
            bars_solved=self.bars_solved,
            bars_null=self.bars_null,
            bars_reused=self.bars_reused,
            bars_skipped=self.bars_skipped,
            bars_undecodable=self.bars_undecodable,
        )


def run_dhan_rolling_greeks_pass(
    *,
    lake_root: Path,
    log: FilteringBoundLogger | None = None,
) -> GreeksEnrichOutcome:
    """Run one fenced rolling-ATM Greeks pass — best-effort, never a crash.

    The Dhan mirror of
    :func:`brutex.data.greeks_enrich_pass.run_greeks_enrich_pass` (the thin
    delegator into the COMMON fence): resolve the risk-free source (the locked
    PR-1 seam — fixed 6.5% default, ``BRUTEX_RISK_FREE_CSV`` override), discover
    the rolling keys, run the sequential driver, log one
    ``dhan.rolling_greeks.done`` with the report counters, and — when overlay
    bars were written — touch the SHARED Greeks freshness marker so a
    concurrent dashboard's caches serve the new Greeks on the next poll. TWO
    deliberate re-raises past the fence: :class:`LakeWriteHalt` and
    :class:`GreeksEnrichPoolBroken` (host conditions → the caller's pull-halt +
    CRITICAL alert). Every other error logs ONE
    ``dhan.rolling_greeks.error`` and returns ``ok=False`` — a failed overlay
    pass never fails the host pull (the 1-minute candles are already durable;
    the overlay is re-derivable, idempotent — Golden Rule #7/#10).

    Returns
    -------
    GreeksEnrichOutcome
        ``ok=True`` + the per-key ``errored`` tally on completion, ``ok=False``
        + ``"ErrorType: message"`` on a whole-pass failure, or
        :data:`GREEKS_NOT_RUN` when there are no rolling keys at all.
    """
    logger = _LOG if log is None else log
    try:
        rate_source = resolve_risk_free_source()
        targets = discover_rolling_option_targets(lake_root)
        if not targets:
            return GREEKS_NOT_RUN
        report = enrich_rolling_greeks_for_targets(
            targets=targets, lake_root=lake_root, rate_source=rate_source
        )
        logger.info(
            "dhan.rolling_greeks.done",
            enriched=report.enriched,
            already_current=report.already_current,
            no_bars=report.no_bars,
            errored=report.errored,
            bars_written=report.bars_written,
            bars_served=report.bars_served,
            bars_solved=report.bars_solved,
            bars_null=report.bars_null,
            bars_reused=report.bars_reused,
            bars_skipped=report.bars_skipped,
            bars_undecodable=report.bars_undecodable,
        )
        if report.bars_written > 0:
            touch_greeks_revision_marker(
                lake_root,
                fields={
                    "writer": "dhan_rolling_greeks_pass",
                    "bars_written": report.bars_written,
                },
                failure_event="dhan.rolling_greeks.revision_marker_failed",
            )
        return GreeksEnrichOutcome(ok=True, error=None, errored_targets=report.errored)
    except (LakeWriteHalt, GreeksEnrichPoolBroken):
        raise
    except Exception as exc:
        logger.error("dhan.rolling_greeks.error", error=str(exc))
        return GreeksEnrichOutcome(
            ok=False, error=f"{type(exc).__name__}: {exc}", errored_targets=0
        )


__all__ = [
    "RollingEnrichStats",
    "RollingGreeksError",
    "RollingGreeksOverlay",
    "RollingGreeksReport",
    "RollingGreeksTarget",
    "discover_rolling_option_targets",
    "enrich_rolling_greeks_for_targets",
    "enrich_rolling_option_bars",
    "read_rolling_option_rows",
    "run_dhan_rolling_greeks_pass",
    "write_rolling_option_greeks",
]
