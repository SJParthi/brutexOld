"""Observe-only Dhan daily-vs-minute consistency observer.

DEC-DHAN-NATIVE-DAILY-001 item 7 (implementing `DEC-SCOPE-MINIMAL-CORE-001`).
After BOTH Dhan spot stages complete (native-daily FIRST, then 1-minute — the
chain in :mod:`brutex.dashboard.dhan_pull`), this observer compares each pulled
index's NATIVE ``1day`` closes against the aggregate (last-by-timestamp) close
of its stored ``1minute`` bars for the OVERLAPPING days of THIS pull's range.

THE LAW (observe-only, Golden Rule #1):

* it NEVER rewrites either side — the native daily partition is vendor truth
  and the 1-minute partition is vendor truth; a divergence is a WARN
  (``dhan.daily_minute_divergence``), never a "heal";
* the tolerance is :data:`~brutex.adapters.dhan.constants.
  DHAN_DAILY_CONSISTENCY_TOLERANCE` (0.005 relative) — PROVISIONAL/UNVERIFIED:
  a vendor daily bar and a minute aggregate legitimately differ (auction
  prints, closing-price rounding), so the figure is a design-time guess, not a
  measured one; a measured tolerance comes later;
* WARNs are capped at :data:`DIVERGENCE_WARN_CAP` per run with ONE suppressed
  summary (``dhan.daily_minute_divergence_suppressed``) — never a log storm;
* the work is BOUNDED to this pull's range: each target reads only the month
  partitions the ``[start, end)`` span covers (the COMMON
  :func:`~brutex.data.spot_daily_resample.ist_span_partition_files` bound), so
  the observer is O(pulled-window months), never O(lake-depth);
* it is CRASH-GUARDED at two levels — a per-target read/compute fault is
  logged + counted (``dhan.daily_minute_consistency_target_error``) and the
  remaining targets still run; the :func:`observe_daily_minute_consistency_guarded`
  wrapper additionally fences the whole observer so a failure can NEVER fail
  the already-successful pull (the ``run_dhan_prebuild`` crash-guard posture);
* the kill switch ``BRUTEX_DHAN_DAILY_CONSISTENCY`` (:func:`consistency_enabled`)
  is FAIL-LOUD on junk (the ``resolve_today_completeness_enabled`` idiom) and
  is resolved ONCE per pull at the chain entry — junk fails loud BEFORE any
  vendor call, never mid-run.

A day with daily-but-no-minute data (or vice versa) is honestly SKIPPED — the
observer compares only days BOTH sides hold; absence is a completeness matter,
not a consistency divergence.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

import polars as pl
import structlog

from brutex.adapters.dhan.constants import DHAN_DAILY_CONSISTENCY_TOLERANCE
from brutex.adapters.dhan.ingest import SPOT_SEGMENT
from brutex.data.spot_daily_resample import ist_span_partition_files

_LOG = structlog.get_logger("brutex.adapters.dhan.daily_consistency")

#: The observer kill switch — unset/blank/on-token = enabled (default ON),
#: off-token = disabled, anything else raises (fail-loud junk — the
#: DEC-RAM-BUDGETS-001 env convention).
DHAN_DAILY_CONSISTENCY_ENV: Final[str] = "BRUTEX_DHAN_DAILY_CONSISTENCY"

#: Per-run WARN cap: at most this many ``dhan.daily_minute_divergence`` lines,
#: then ONE suppressed summary carrying the true remainder (the house capped-WARN
#: idiom — divergences past the cap stay COUNTED, never dropped).
DIVERGENCE_WARN_CAP: Final[int] = 20

_ENV_ON_TOKENS: Final[frozenset[str]] = frozenset({"", "1", "on", "true", "yes"})
_ENV_OFF_TOKENS: Final[frozenset[str]] = frozenset({"0", "off", "false", "no"})


def consistency_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the ``BRUTEX_DHAN_DAILY_CONSISTENCY`` kill switch — fail-loud.

    unset / ``""`` / ``"1"`` / ``"on"`` / ``"true"`` / ``"yes"``
    (case-insensitive, whitespace-stripped) → ``True`` (default-ENABLED —
    observing is free-ish and observe-only); ``"0"`` / ``"off"`` / ``"false"``
    / ``"no"`` → ``False``. Anything else raises ``ValueError`` — junk is never
    silently coerced (the fail-loud env convention; resolved ONCE at the pull
    chain's entry so junk fails BEFORE any vendor call).

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.
    """
    mapping = os.environ if env is None else env
    raw = mapping.get(DHAN_DAILY_CONSISTENCY_ENV, "")
    token = raw.strip().lower()
    if token in _ENV_ON_TOKENS:
        return True
    if token in _ENV_OFF_TOKENS:
        return False
    raise ValueError(
        f"{DHAN_DAILY_CONSISTENCY_ENV}={raw!r} is not a recognised switch value; "
        f"expected one of '1', 'on', 'true', 'yes' (enable — the default when unset) "
        f"or '0', 'off', 'false', 'no' (disable)"
    )


@dataclass(frozen=True, slots=True)
class DailyMinuteDivergence:
    """One symbol·day whose native daily close diverges past the tolerance.

    Attributes
    ----------
    exchange, symbol, day
        The lake coordinates (``bars/<exchange>/INDEX/<symbol>``) + the IST day.
    daily_close
        The NATIVE ``1day`` close (vendor truth, pulled first in the chain).
    minute_close
        The stored ``1minute`` aggregate close (the day's last bar by timestamp).
    rel_diff
        ``|daily − minute| / |daily|`` (``inf`` for a zero daily close — the
        divergence predicate itself is division-free, see
        :func:`observe_daily_minute_consistency`).
    """

    exchange: str
    symbol: str
    day: date
    daily_close: float
    minute_close: float
    rel_diff: float


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    """The observer's honest tally — comparisons made, divergences found, faults counted.

    Attributes
    ----------
    compared_days
        Symbol·days where BOTH a native daily close and a minute aggregate
        close existed inside ``[start, end)`` (the only comparable population).
    divergences
        Every divergence found (NOT capped — the WARN cap bounds log lines,
        never the record).
    warned, suppressed
        How many divergences were WARNed individually vs folded into the ONE
        suppressed summary.
    targets_failed
        ``{symbol → error_type}`` for targets whose read/compute RAISED (the
        per-target crash guard — the remaining targets still ran).
    """

    compared_days: int
    divergences: tuple[DailyMinuteDivergence, ...]
    warned: int
    suppressed: int
    targets_failed: Mapping[str, str]


def _last_close_by_day(files: Sequence[str]) -> dict[date, float]:
    """``{IST day → last close by timestamp}`` over the given month-partition files.

    Reads ONLY the ``timestamp`` + ``close`` columns, sorts by timestamp (so
    ``last()`` is the day's final bar, never a positional artifact), and folds
    each UTC instant to its IST day via the fixed +05:30 arithmetic the
    resampler uses (a midnight-IST daily stamp lands on its own IST date even
    though it lives in the previous UTC day). Empty *files* → ``{}``.
    """
    if not files:
        return {}
    frame = pl.read_parquet(list(files), columns=["timestamp", "close"])
    if frame.is_empty():
        return {}
    folded = (
        frame.sort("timestamp")
        .with_columns(
            (pl.col("timestamp") + pl.duration(hours=5, minutes=30)).dt.date().alias("_day")
        )
        .group_by("_day")
        .agg(pl.col("close").last().alias("_close"))
    )
    return {row["_day"]: float(row["_close"]) for row in folded.iter_rows(named=True)}


def observe_daily_minute_consistency(
    *,
    dhan_root: Path,
    targets: Sequence[tuple[str, str]],
    start: date,
    end: date,
    tolerance: float = DHAN_DAILY_CONSISTENCY_TOLERANCE,
    warn_cap: int = DIVERGENCE_WARN_CAP,
) -> ConsistencyReport:
    """Compare native ``1day`` closes vs the ``1minute`` aggregate for this pull's range.

    For each ``(exchange, symbol)`` target the observer reads the Dhan lake's
    ``bars/<exchange>/INDEX/<symbol>/{1day,1minute}`` month partitions BOUNDED
    to the ``[start, end)`` span (:func:`~brutex.data.spot_daily_resample.
    ist_span_partition_files` — O(pulled-window months)), folds each side to
    ``{IST day → last close}``, and flags a day divergent when
    ``|daily − minute| > tolerance × |daily|`` (division-free, so a zero daily
    close needs no special-case guard: any non-zero minute close diverges,
    ``0 == 0`` does not). Days present on only ONE side are skipped honestly —
    absence is a completeness matter, not a consistency divergence.

    Emits at most *warn_cap* ``dhan.daily_minute_divergence`` WARNs plus ONE
    ``dhan.daily_minute_divergence_suppressed`` summary; a per-target
    read/compute fault is logged (``dhan.daily_minute_consistency_target_error``)
    + counted and the remaining targets still run. NEVER rewrites either side.

    Parameters
    ----------
    dhan_root
        The Dhan feed lake root (``<lake_root>/feeds/dhan``).
    targets
        ``(exchange, symbol)`` pairs — the pulled index partitions.
    start, end
        The pull's half-open IST ``[start, end)`` range (the comparison bound).
    tolerance
        The relative divergence tolerance (default the PROVISIONAL/UNVERIFIED
        :data:`~brutex.adapters.dhan.constants.DHAN_DAILY_CONSISTENCY_TOLERANCE`).
    warn_cap
        The per-run individual-WARN cap (default :data:`DIVERGENCE_WARN_CAP`).
    """
    compared = 0
    divergences: list[DailyMinuteDivergence] = []
    targets_failed: dict[str, str] = {}
    for exchange, symbol in targets:
        symbol_base = dhan_root / "bars" / exchange / SPOT_SEGMENT / symbol
        try:
            daily_map = _last_close_by_day(
                ist_span_partition_files(symbol_base / "1day", start, end)
            )
            minute_map = _last_close_by_day(
                ist_span_partition_files(symbol_base / "1minute", start, end)
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught — per-target isolation, observe-only
            _LOG.warning(
                "dhan.daily_minute_consistency_target_error",
                symbol=symbol,
                exchange=exchange,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            targets_failed[symbol] = type(exc).__name__
            continue
        for day in sorted(daily_map):
            if not start <= day < end:
                continue
            minute_close = minute_map.get(day)
            if minute_close is None:
                continue
            daily_close = daily_map[day]
            compared += 1
            if abs(daily_close - minute_close) > tolerance * abs(daily_close):
                rel = (
                    abs(daily_close - minute_close) / abs(daily_close)
                    if daily_close != 0.0
                    else float("inf")
                )
                divergences.append(
                    DailyMinuteDivergence(
                        exchange=exchange,
                        symbol=symbol,
                        day=day,
                        daily_close=daily_close,
                        minute_close=minute_close,
                        rel_diff=rel,
                    )
                )
    warned = 0
    for divergence in divergences:
        if warned >= warn_cap:
            break
        _LOG.warning(
            "dhan.daily_minute_divergence",
            symbol=divergence.symbol,
            exchange=divergence.exchange,
            day=divergence.day.isoformat(),
            daily_close=divergence.daily_close,
            minute_close=divergence.minute_close,
            rel_diff=divergence.rel_diff,
            tolerance=tolerance,
        )
        warned += 1
    suppressed = len(divergences) - warned
    if suppressed > 0:
        _LOG.warning(
            "dhan.daily_minute_divergence_suppressed",
            suppressed=suppressed,
            total=len(divergences),
            cap=warn_cap,
        )
    return ConsistencyReport(
        compared_days=compared,
        divergences=tuple(divergences),
        warned=warned,
        suppressed=suppressed,
        targets_failed=targets_failed,
    )


def observe_daily_minute_consistency_guarded(
    *,
    dhan_root: Path,
    targets: Sequence[tuple[str, str]],
    start: date,
    end: date,
    tolerance: float = DHAN_DAILY_CONSISTENCY_TOLERANCE,
    warn_cap: int = DIVERGENCE_WARN_CAP,
) -> ConsistencyReport | None:
    """The crash-guarded observer fence — a failure NEVER fails the pull.

    Delegates to :func:`observe_daily_minute_consistency`; ANY raise is folded
    into one ``dhan.daily_minute_consistency_error`` WARN + ``None`` (the
    ``run_dhan_prebuild`` best-effort posture — the observer is diagnostics,
    the pull result is already durable).
    """
    try:
        return observe_daily_minute_consistency(
            dhan_root=dhan_root,
            targets=targets,
            start=start,
            end=end,
            tolerance=tolerance,
            warn_cap=warn_cap,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught — observe-only fence
        _LOG.warning(
            "dhan.daily_minute_consistency_error",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None


__all__ = [
    "DHAN_DAILY_CONSISTENCY_ENV",
    "DIVERGENCE_WARN_CAP",
    "ConsistencyReport",
    "DailyMinuteDivergence",
    "consistency_enabled",
    "observe_daily_minute_consistency",
    "observe_daily_minute_consistency_guarded",
]
