"""Day-scoped incremental prebuild — the heal-recall composition, verbatim.

DEC-CROSSVERIFY-DAY-DERIVE-001 (§6): a composition of EXISTING heal-recall
primitives ONLY — NO new stamping rule (the stamp redesign is the named
follow-up DEC-PREBUILD-DAY-SCOPED-STAMP-001). Per pulled/exported coordinate,
TWO derives:

1. **Intraday 19 TFs** — ``materialize_lake_timeframes(months={(day.y,
   day.m)}, include_daily=False, incremental=False)``: month-bounded read
   (an NSE session never crosses a UTC month), latest-wins writes converge in
   place; ``months=`` structurally forces no-skip/no-stamp — the "partial
   derive must never stamp" invariant holds by CONSTRUCTION (``months= +
   include_daily=True`` RAISES upstream, hence the two-call shape).
2. **Derived 1day** — ``derive_daily_for_targets`` over a ±1-day-padded
   window read (the midnight-IST straddle), latest-wins corrects a partial
   predecessor; F&O coordinates ride the OI-carry positional resampler.

Failure isolation: per-symbol try/except (the gated-prebuild quarantine
posture) — counted + capped-logged, never blocks the 1-minute export;
``LakeWriteHalt`` re-raises (a full disk halts the run).

The FNO arm (``PrebuildKind.FUTURES`` + the OI-carry positional 1day
resampler) is STRUCTURAL since DEC-CROSSVERIFY-SPOT-ONLY-001 (MIGRATION.md
#1788) — unreachable from the spot-only producer's enumeration, retained
(direct-unit-covered via a constructed FNO identity) for the contract's
possible re-expansion.

Honest cost (Golden Rule #10): O(touched symbols × one UTC month of 1-minute
bars) read × 19 TF resamples + O(symbols × 3-day window) for 1day — NOT
O(1), NOT strictly O(new day) (the month read grows through the month), and
NEVER O(full history). The derive stamp stays STALE for every touched symbol
(bounded derives never stamp), so the next nightly full prebuild re-derives
their full history anyway — exactly today's post-pull behavior; this leg
adds a bounded early derive and NO new nightly cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.data.errors import LakeWriteHalt

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    from brutex.crossverify.canonical import CanonicalIdentity

_LOG = structlog.get_logger("brutex.crossverify.prebuild_leg")

#: Failed-name report cap (the true count always rides ``failed``).
FAILED_NAMES_CAP: Final[int] = 10

#: Per-symbol failure WARN cap per run (the gated-prebuild posture).
MAX_FAILURE_WARNS: Final[int] = 20


@dataclass(frozen=True, slots=True)
class DeriveOutcome:
    """The prebuild leg's tally."""

    derived: int
    failed: int
    failed_names: tuple[str, ...]  # capped at FAILED_NAMES_CAP; ``failed`` is the truth


def _derive_one(
    *,
    lake_root: Path,
    day: date,
    identity: CanonicalIdentity,
    before: date,
    materialize: Callable[..., Any],
    derive_daily: Callable[..., Any],
    read_window_factory: Callable[..., Any],
    write_daily_factory: Callable[..., Any],
) -> None:
    """Run the two bounded derives for ONE coordinate (§6 composition)."""
    from brutex.data.spot_daily_resample import DailyDeriveTarget
    from brutex.data.timeframe_materialize import MATERIALIZABLE_TFS, PrebuildKind

    kind = PrebuildKind.FUTURES if identity.lake_segment == "FNO" else PrebuildKind.SPOT
    materialize(
        lake_root=lake_root,
        exchange=identity.exchange,
        segment=identity.lake_segment,
        symbol=identity.lake_symbol,
        kind=kind,
        tfs=MATERIALIZABLE_TFS,
        include_daily=False,
        months={(day.year, day.month)},
        incremental=False,
    )
    target = DailyDeriveTarget(
        exchange=identity.exchange,
        segment=identity.lake_segment,
        symbol=identity.lake_symbol,
        start=day - timedelta(days=1),
        end=day + timedelta(days=1),
    )
    resample_fn = _fno_daily_resample_fn() if identity.lake_segment == "FNO" else None
    derive_daily(
        targets=[target],
        build_read_1m=lambda t: read_window_factory(
            lake_root=lake_root,
            exchange=t.exchange,
            segment=t.segment,
            symbol=t.symbol,
            start=t.start,
            end=t.end,
            include_oi=t.segment == "FNO",
        ),
        build_write_1day=lambda t: write_daily_factory(
            lake_root=lake_root, exchange=t.exchange, segment=t.segment, symbol=t.symbol
        ),
        before=before,
        resample_fn=resample_fn,
    )


def _fno_daily_resample_fn() -> Callable[..., Any]:
    """The OI-carry positional F&O daily resampler (the heal-recall arm)."""
    from brutex.data.fno_daily_resample import resample_1m_to_fno_daily_positional

    return resample_1m_to_fno_daily_positional


def run_prebuild_leg(
    *,
    lake_root: Path,
    day: date,
    coordinates: Sequence[CanonicalIdentity],
    now_ist: Callable[[], datetime],
    materialize: Callable[..., Any] | None = None,
    derive_daily: Callable[..., Any] | None = None,
    read_window_factory: Callable[..., Any] | None = None,
    write_daily_factory: Callable[..., Any] | None = None,
) -> DeriveOutcome:
    """Derive the day's 19 intraday TFs + 1day for every coordinate — isolated.

    A per-symbol failure is counted + capped-logged
    (``crossverify.prebuild_symbol_failed``) and NEVER blocks the export (the
    export reads 1-minute only); :class:`LakeWriteHalt` re-raises. The
    injectable callables default to the REAL primitives (lazy imports).
    """
    if materialize is None:
        from brutex.data.timeframe_materialize import materialize_lake_timeframes

        materialize = materialize_lake_timeframes
    if derive_daily is None:
        from brutex.data.spot_daily_resample import derive_daily_for_targets

        derive_daily = derive_daily_for_targets
    if read_window_factory is None:
        from brutex.data.spot_daily_resample import make_lake_read_1m_window

        read_window_factory = make_lake_read_1m_window
    if write_daily_factory is None:
        from brutex.data.spot_daily_resample import make_lake_write_1day

        write_daily_factory = make_lake_write_1day
    from brutex.calendars.nse import daily_derive_cutoff

    before = daily_derive_cutoff(now_ist())
    derived = 0
    failed_names: list[str] = []
    for identity in coordinates:
        try:
            _derive_one(
                lake_root=lake_root,
                day=day,
                identity=identity,
                before=before,
                materialize=materialize,
                derive_daily=derive_daily,
                read_window_factory=read_window_factory,
                write_daily_factory=write_daily_factory,
            )
        except LakeWriteHalt:
            raise  # a full disk is a pull-wide stop, never quarantined
        except Exception as exc:  # per-symbol quarantine — counted, capped-logged
            failed_names.append(identity.canonical_symbol)
            if len(failed_names) <= MAX_FAILURE_WARNS:
                _LOG.warning(
                    "crossverify.prebuild_symbol_failed",
                    symbol=repr(identity.canonical_symbol),  # vendor-derived — repr()'d (#907)
                    day=day.isoformat(),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        else:
            derived += 1
    return DeriveOutcome(
        derived=derived,
        failed=len(failed_names),
        failed_names=tuple(failed_names[:FAILED_NAMES_CAP]),
    )


__all__ = ["FAILED_NAMES_CAP", "MAX_FAILURE_WARNS", "DeriveOutcome", "run_prebuild_leg"]
