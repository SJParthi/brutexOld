"""Observed-sessions drift reconciler — a REMOVE-ONLY overlay on the derived NSE calendar.

DEC-CALENDAR-OBSERVED-SESSIONS-001 (Session D PR-1). The derived NSE calendar can
drift from vendor reality: a live Groww pull served **20** distinct Jan-2026
trading sessions while the calendar derives **21** (the DEC-HARDENING-LEFTOVERS-001
honesty note at ``brutex/calendars/nse.py`` — MIGRATION.md #1149). The missing
holiday's DATE is UNVERIFIED and per Golden Rule #1 must never be fabricated into
``HOLIDAYS_2026``. This module closes the OPERATIONAL gap instead: it detects, from
the lake's own evidence, that a derived-trading-day was OBSERVABLY not traded, and
records it in a labeled, human-auditable overlay sidecar that consumers subtract
from the derived calendar.

The overlay asserts ONLY "the market observably did not trade this day" — never
"holiday X". The named-holiday gap stays closable only by the official NSE circular
or a live vendor probe.

Hard bounds (operator decree, each STRUCTURAL, each test-pinned)
----------------------------------------------------------------
* **Remove-only.** The single consumption primitive is
  :func:`is_trading_day_observed` = ``is_trading_day(d) AND d not in observed`` —
  there is NO ADD channel in the data shape, the API, or any consumer. An overlay
  member can only flip ``True -> False``, never ``False -> True``.
* **Evidence-gated.** A derived trading day is flagged iff (a) ZERO bars across ALL
  3 engine indices (``ENGINE_INDEX_COORDS``) for that day, (b) at least
  :data:`MIN_CLEAN_SUCCESS_WINDOWS` clean-``SUCCESS`` 1-minute ledger window(s)
  covering it PER index, (c) no ``FAILED`` ledger row touching it (ANY
  market/symbol/interval — a deliberately over-broad veto), and (d) all 3 indices
  carry bars elsewhere in the month (corroboration: an absent month file is data
  ABSENCE, not holiday evidence).
* **Bounded.** At most :data:`MAX_FLAGS_PER_MONTH` flags per month (more admitted
  candidates than that refuses the WHOLE month — a >3-holiday month is more
  plausibly a data disaster than drift) and at most
  :data:`MAX_OVERLAY_DAYS_PER_YEAR` overlay days per calendar year; over-cap
  refuses LOUDLY (structlog error, NO overlay write). Fully-past months only.
* **Labeled.** Every entry carries ``"observed-not-official"`` AND a canonical
  ISO-format ``date``; the loader REJECTS an unlabeled or non-canonical entry.
  Honest limit: a hand-edited entry that COPIES the label + canonical form DOES
  load — that is the same single-operator trust envelope as the documented
  delete escape hatch (remove-only means a hand-add can only REMOVE sessions),
  and the month/year caps are enforced at DETECTION time
  (:func:`classify_month_drift`), not re-checked at load.

Why ``MIN_CLEAN_SUCCESS_WINDOWS == 1`` (not the sketched 2)
-----------------------------------------------------------
"≥2 independent clean fetches of the same window" is UNSATISFIABLE by
construction: a ``SUCCESS`` ledger row is settled, and
``WindowLedger.is_skippable`` short-circuits every later pull BEFORE any vendor
call, so a second independent clean fetch of the SAME window never happens. The
real multiplicity lives ACROSS indices: 3 index series × ≥1 clean window each =
3 independent clean observations of the day.

Self-healing (refuter fold R2)
------------------------------
:func:`detect_session_drift` writes whenever the recomputed month entry-set
DIFFERS from the stored month entry-set — INCLUDING the transition to the empty
set — so a stale entry for a day that later gains real bars (a re-pull /
``reopen-skipped``) is REMOVED on the next detection, never asserted forever
against a lake that refutes it (Golden Rule #1). Cap refusals stay strictly
no-write. A re-run over unchanged evidence preserves each entry's ``detected_at``
(refuter fold R6), so the production re-run is genuinely byte-idempotent.

Honest envelope (Golden Rule #10)
---------------------------------
* A simultaneous same-day vendor hole across all 3 independently-pulled engine
  indices — with clean SUCCESS windows and zero FAILED rows — is indistinguishable
  from a real holiday. Bounded by the 3-index conjunction + month corroboration +
  FAILED veto + both caps + the human-editable labeled sidecar + remove-only
  consumers whose 5-session tolerances never hard-fail on one day.
* ``seed_enclosed_windows`` is a SPAN test, not a density test — a seeded SUCCESS
  can supply "clean evidence" over an interior hole. Month corroboration blunts
  but does not eliminate this; accepted residual.
* The over-broad FAILED veto means a mid-backfill lake with lingering FAILED F&O
  rows may NEVER flag anything until those settle (refuter fold R7) — the veto
  outcome is logged at WARNING so the operator sees WHY, and over-veto is the safe
  direction for a remove-only overlay (a missed flag costs nothing new).
* The sidecar read-merge-write is NOT isolated against a concurrent detection run
  (crash-atomic only — ``atomic_write_simple``). The sole production invocation
  surface is the manual ``brutex detect-session-drift`` CLI command (single
  operator), so the race is an accepted, ledgered envelope item (refuter fold
  R11), not mechanised away.
* The overlay is loaded once per report/pull/process — no live invalidation.
* A VERIFIED special weekend trading session (``SPECIAL_WEEKEND_TRADING_SESSIONS``
  — e.g. the 2026-02-01 Budget Sunday) is a detector CANDIDATE like any other
  derived session: ``trading_days_between`` includes it, so a bar-less special
  session under clean evidence CAN be flagged into the overlay. This is
  design-consistent (the observation "no bars were served" is honest, consumers
  behave better with the flag than without on a bar-less day, and a later
  backfill self-heals the entry) but it IS the overlay overriding a
  circular-verified calendar entry — stated, not hidden.

Import layering: this module imports ONLY stdlib + structlog +
``brutex.calendars.nse`` at module level; every ``brutex.data.*`` /
``brutex.adapters.*`` read is a function-local import (those packages import
``brutex.calendars.nse`` at module level — a top-level import here would create a
package cycle). ``brutex/calendars/__init__.py`` deliberately does NOT re-export
this module for the same reason.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from stat import S_ISREG
from typing import Any, Final

import structlog

from brutex.calendars.calendar_rust import (
    _LAW_CLASSIFY,
    _LAW_OBSERVED,
    LawOutcome,
    Value,
    routed,
    unwrap,
    warn_fallback,
)
from brutex.calendars.nse import (
    is_holiday_data_available,
    is_trading_day,
    is_trading_day_checked,
    trading_days_between,
)

_LOG = structlog.get_logger(__name__)

__all__ = [
    "MAX_FLAGS_PER_MONTH",
    "MAX_OVERLAY_BYTES",
    "MAX_OVERLAY_DAYS_PER_YEAR",
    "MIN_CLEAN_SUCCESS_WINDOWS",
    "OBSERVED_LABEL",
    "OBSERVED_OVERLAY_DIRNAME",
    "OBSERVED_OVERLAY_FILENAME",
    "OBSERVED_OVERLAY_VERSION",
    "DayEvidence",
    "DriftReport",
    "DriftVerdict",
    "ObservedHoliday",
    "ObservedSessionsError",
    "classify_month_drift",
    "detect_session_drift",
    "is_trading_day_checked_observed",
    "is_trading_day_observed",
    "load_observed_holidays",
    "overlay_path",
]

#: Sidecar directory under the lake root. OUTSIDE ``bars/`` + ``options/`` and not
#: ``*.parquet``, so no lake scanner ever sees it (the ``partition_lock`` ``.locks/``
#: placement argument).
OBSERVED_OVERLAY_DIRNAME: Final[str] = "calendar_overlays"
OBSERVED_OVERLAY_FILENAME: Final[str] = "observed_holidays.json"
OBSERVED_OVERLAY_VERSION: Final[int] = 1
#: Stamped on EVERY entry; the loader REJECTS an entry without it. The overlay
#: asserts an OBSERVATION, never an official NSE circular fact (Golden Rule #1).
OBSERVED_LABEL: Final[str] = "observed-not-official"
#: Hard bound: more than this many admitted candidates in ONE month refuses the
#: WHOLE month (a data disaster is more plausible than 4+ unlisted holidays).
MAX_FLAGS_PER_MONTH: Final[int] = 3
#: Hard bound: the overlay may never carry more than this many days per calendar
#: year; an over-cap detection refuses LOUDLY with NO write.
MAX_OVERLAY_DAYS_PER_YEAR: Final[int] = 8
#: Minimum clean-``SUCCESS`` 1-minute ledger windows covering the day, PER engine
#: index (see the module docstring for why ≥2 is unsatisfiable).
MIN_CLEAN_SUCCESS_WINDOWS: Final[int] = 1
#: Bounded sidecar read (review rounds 4 + 5 — the DEC-LAKE-STATS-MANIFEST-001
#: #1614 bounded-shard-read precedent): the overlay contract is TINY — the
#: honest FULL-COVERAGE maximum is :data:`MAX_OVERLAY_DAYS_PER_YEAR` (8)
#: entries × 7 covered calendar years (2020-2026) = 56 entries ≈ ~22 KB via
#: this module's own serializer, so 1 MiB is ~47× that maximum (review
#: round 5 corrected the prior "~1000×" arithmetic overstatement; the bound
#: itself is unchanged). An oversized file is junk — fail closed to the empty
#: overlay, never read whole (a multi-GB plant must not be slurped into
#: memory by a dashboard page / pull / sweep launch).
MAX_OVERLAY_BYTES: Final[int] = 1 << 20

#: The corroboration bar: ALL engine indices must carry bars somewhere in the
#: month for a zero-bar day to be admissible (len(``ENGINE_INDEX_COORDS``) — an
#: absent index month file is data ABSENCE, not holiday evidence).
_ENGINE_INDEX_COUNT: Final[int] = 3

#: The fetched spot interval whose ledger windows count as evidence
#: (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 — 1-minute is the ONLY vendor-fetched spot
#: granularity, so only its windows are real fetch evidence).
_EVIDENCE_INTERVAL: Final[str] = "1minute"

# ── structlog event names (all test-pinned) ─────────────────────────────────
DRIFT_DETECTED_EVENT: Final[str] = "observed_sessions.drift_detected"
FLAG_VETOED_EVENT: Final[str] = "observed_sessions.flag_vetoed_failed"
NO_EVIDENCE_EVENT: Final[str] = "observed_sessions.no_ledger_evidence"
MONTH_CAP_EVENT: Final[str] = "observed_sessions.month_cap_exceeded"
YEAR_CAP_EVENT: Final[str] = "observed_sessions.year_cap_exceeded"
OVERLAY_WRITTEN_EVENT: Final[str] = "observed_sessions.overlay_written"
OVERLAY_LOAD_FAILED_EVENT: Final[str] = "observed_sessions.overlay_load_failed"
OVERLAY_WRITE_FAILED_EVENT: Final[str] = "observed_sessions.overlay_write_failed"
LEDGER_UNREADABLE_EVENT: Final[str] = "observed_sessions.ledger_unreadable"


class ObservedSessionsError(Exception):
    """The drift reconciler could not complete (sidecar write failed / ledger unreadable).

    Wraps the raw ``OSError`` where one exists. Deliberately NOT a
    :class:`~brutex.data.errors.LakeWriteHalt` — the reconciler is never on the
    pull path, so its disk-full must not mis-escalate into a pull-wide stop.
    """


@dataclass(frozen=True, slots=True)
class ObservedHoliday:
    """One overlaid day + the evidence that flagged it (always observed-not-official)."""

    day: date
    detected_at: str  # ISO-8601 UTC of the detection run that first flagged it
    zero_bar_indices: tuple[str, ...]  # e.g. ("NSE/INDEX/NIFTY", ...)
    clean_success_windows: int  # MIN over the 3 indices of SUCCESS windows covering day
    label: str = OBSERVED_LABEL


@dataclass(frozen=True, slots=True)
class DayEvidence:
    """Pure-core input — one row per derived trading day of the scanned month."""

    day: date
    #: (a) zero bars for the day in ALL 3 engine 1-minute series.
    zero_bars_all_indices: bool
    #: Corroboration: engine indices with >=1 bar SOMEWHERE in the month.
    indices_with_month_data: int
    #: (b) MIN over the 3 indices of clean-SUCCESS 1-minute windows covering day.
    clean_success_windows: int
    #: Veto: ANY FAILED ledger row (any market/symbol/interval) covering day.
    failed_windows_touching: int


@dataclass(frozen=True, slots=True)
class DriftVerdict:
    """Pure-core output of :func:`classify_month_drift`."""

    month: date  # normalized to day=1
    candidates: tuple[date, ...]  # zero-bar derived trading days (pre-evidence)
    flagged: tuple[ObservedHoliday, ...]  # passed evidence + veto + caps
    vetoed_failed: tuple[date, ...]  # candidates killed by the FAILED veto
    no_evidence: tuple[date, ...]  # candidates lacking clean-SUCCESS cover / corroboration
    over_month_cap: bool  # >MAX_FLAGS_PER_MONTH admitted -> WHOLE month refused
    over_year_cap: bool  # year total would exceed MAX_OVERLAY_DAYS_PER_YEAR -> refused


@dataclass(frozen=True, slots=True)
class DriftReport:
    """:func:`detect_session_drift` output."""

    verdict: DriftVerdict
    overlay_path: str
    wrote_overlay: bool


def overlay_path(lake_root: Path) -> Path:
    """The overlay sidecar path under *lake_root* (one file per lake)."""
    return lake_root / OBSERVED_OVERLAY_DIRNAME / OBSERVED_OVERLAY_FILENAME


def is_trading_day_observed(d: date, *, observed: AbstractSet[date]) -> bool:
    """Derived-calendar AND NOT overlaid — the ONE remove-only consumption primitive.

    ``is_trading_day(d) and d not in observed``: an overlay member can only flip a
    verdict ``True -> False``; no input can flip ``False -> True`` (a weekend /
    listed holiday stays non-trading regardless of the overlay).
    """
    return is_trading_day(d) and d not in observed


def is_trading_day_checked_observed(d: date, *, observed: AbstractSet[date]) -> bool:
    """Strict twin of :func:`is_trading_day_observed`.

    Raises :class:`~brutex.calendars.nse.HolidayDataUnavailableError` exactly like
    the underlying checked variant (an out-of-coverage weekday refuses to guess).
    """
    return is_trading_day_checked(d) and d not in observed


def _valid_entry_day(entry: Any) -> date | None:
    """Parse one sidecar entry into its overlaid date, or ``None`` when invalid.

    An entry is valid iff it is a mapping whose ``date`` is the CANONICAL ISO
    spelling (``YYYY-MM-DD`` — exactly ``day.isoformat()``) and whose ``label``
    is exactly :data:`OBSERVED_LABEL` — the loader-side half of the labeled
    bound (an unlabeled entry is REJECTED). Canonical-form-only (hostile-review
    fold): Python 3.11+ ``date.fromisoformat`` also parses compact spellings
    like ``"20260513"``, which the reconciler's string-prefix month/year
    grouping cannot see — such a hand-edited twin entry would evade the year
    cap and never self-heal, so any non-canonical spelling is rejected.
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("label") != OBSERVED_LABEL:
        return None
    raw = entry.get("date")
    if not isinstance(raw, str):
        return None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    if raw != day.isoformat():
        return None
    return day


def _bounded_sidecar_read(path: Path) -> bytes:
    """Read at most :data:`MAX_OVERLAY_BYTES` + 1 bytes of the sidecar (REGULAR files only).

    The FULL #1614 shape (DEC-LAKE-STATS-MANIFEST-001, its rounds 4 + 6): the
    one-byte over-cap probe lets the caller distinguish "exactly at cap"
    (parse it) from "over cap" (reject it) without ever reading an
    out-of-band multi-GB plant whole, AND the open is ``O_NONBLOCK`` +
    ``S_ISREG``-checked on the OPEN descriptor — POSIX ``open(2)`` with
    ``O_RDONLY`` on a writer-less FIFO blocks FOREVER, BEFORE any caller
    belt could fire, so a FIFO planted at the overlay path wedged every
    consumer (dashboard page / pull / sweep launch) with no deadline
    (review round 5; round 4 had adopted only the bounded-read half of the
    precedent it cited). A non-regular inode (FIFO / device / socket /
    directory — ``os.open`` on a directory SUCCEEDS on POSIX, unlike
    ``Path.open``) raises ``OSError``, which — like any other ``OSError``
    here — propagates to the caller's belt (loader: WARN + empty overlay;
    merge reader: ``[]``). A regular file ignores ``O_NONBLOCK``, so the
    bounded read is unchanged.
    """
    # O_NONBLOCK: the FIFO-hang guard (see the docstring above).
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    # fd ownership handoff (the #1614 fd close discipline): ``owned`` is True
    # exactly while THIS frame must close the descriptor; ``os.fdopen``
    # transfers ownership to the BufferedReader, which closes it internally —
    # no leak on the not-regular raise, no double-close on the served path.
    owned = True
    try:
        if not S_ISREG(os.fstat(fd).st_mode):
            # ``fstat`` on the OPEN descriptor — no check-to-read race.
            raise OSError("non-regular sidecar inode (FIFO/device/socket/dir) rejected")
        with os.fdopen(fd, "rb") as handle:
            # The file object now owns (and closes) the descriptor.
            owned = False
            return handle.read(MAX_OVERLAY_BYTES + 1)
    finally:
        if owned:
            os.close(fd)


def load_observed_holidays(lake_root: Path) -> frozenset[date]:
    """Read the overlay sidecar into a frozenset of overlaid dates. NEVER raises.

    A missing file is an empty overlay (the steady state — no drift detected). A
    malformed / unreadable / wrong-version / mislabeled / oversized sidecar logs
    ONE :data:`OVERLAY_LOAD_FAILED_EVENT` WARNING and degrades to the empty
    overlay — an unreadable overlay must never break a dashboard page, a pull,
    or a sweep launch; the consumer simply falls back to the plain derived
    calendar. Review round 4 (the #1558-r2/#1614-r1 precedent class): the read
    is BOUNDED at :data:`MAX_OVERLAY_BYTES` and the parse arm also folds a
    nesting-bomb ``RecursionError`` + an invalid-UTF-8 ``UnicodeDecodeError``
    into the same WARN + empty-overlay degrade (both previously ESCAPED,
    falsifying this docstring's never-raises claim). Review round 5: a
    NON-REGULAR inode (FIFO/device/socket) planted at the overlay path fails
    closed through the SAME ``OSError`` belt instead of blocking ``open(2)``
    forever (see :func:`_bounded_sidecar_read` — an indefinite hang is worse
    than a raise and equally falsified the never-breaks claim).
    """
    path = overlay_path(lake_root)
    try:
        raw = _bounded_sidecar_read(path)
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        _LOG.warning(OVERLAY_LOAD_FAILED_EVENT, path=str(path), error=str(exc))
        return frozenset()
    if len(raw) > MAX_OVERLAY_BYTES:
        _LOG.warning(
            OVERLAY_LOAD_FAILED_EVENT,
            path=str(path),
            error=f"oversized sidecar (> {MAX_OVERLAY_BYTES} bytes) rejected",
        )
        return frozenset()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        _LOG.warning(OVERLAY_LOAD_FAILED_EVENT, path=str(path), error=str(exc))
        return frozenset()
    days, reason = _routed_classify_observed_payload(payload, raw=raw)
    if days is None:
        # The LOADER owns the WARN wording (the pure classifier is log-free so
        # it can run during burn-in). A crate REJECT (``None``) abstains to the
        # oracle for the precise sub-reason — the reject decision always serves
        # the Python oracle; only ACCEPT is Rust-served + burn-in-verified.
        _LOG.warning(OVERLAY_LOAD_FAILED_EVENT, path=str(path), error=reason)
        return frozenset()
    return days


def _classify_observed_payload(payload: object) -> tuple[frozenset[date] | None, str | None]:
    """PURE, LOG-FREE overlay validation (the LIVE oracle) — the whole-accept /
    whole-reject decision half of :func:`load_observed_holidays`.

    Returns ``(frozenset(days), None)`` when the payload passes EVERY rule
    (``dict`` root, ``version == OBSERVED_OVERLAY_VERSION`` under Python ``==``
    punning, ``entries`` a list, every entry labeled + canonical via
    :func:`_valid_entry_day`), or ``(None, reason)`` on the first failure — one
    bad entry rejects the WHOLE overlay. Log-free so the facade may run it during
    burn-in without a spurious WARN; the LOADER emits the ``reason`` WARN.
    """
    if not isinstance(payload, dict) or payload.get("version") != OBSERVED_OVERLAY_VERSION:
        return None, "wrong shape or version"
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None, "entries not a list"
    days: set[date] = set()
    for entry in entries:
        day = _valid_entry_day(entry)
        if day is None:
            return None, "invalid or unlabeled entry rejected"
        days.add(day)
    return frozenset(days), None


def _materialize_observed(
    raw_return: object,
) -> LawOutcome[tuple[frozenset[date] | None, str | None]] | None:
    """Re-hydrate the crate's ``parse_observed_sidecar`` return.

    ``None`` = a REJECT decision (one bad entry rejects all) → ABSTAIN to the
    oracle for the precise WARN sub-reason. A ``list[str]`` of sorted unique ISO
    days is the ACCEPT verdict → ``Value((frozenset(days), None))``. A wrong
    SHAPE is refuse-poison; a malformed ISO string reaching
    :meth:`date.fromisoformat` raises ``ValueError`` UP into the facade degrade
    set. (The crate RAISES ``ValueError`` on a serde parse divergence — the real
    parse-engine abstain — which the facade degrade set catches; only the
    whole-reject decision arrives here as ``None``.)
    """
    if raw_return is None:
        return None
    if not isinstance(raw_return, list) or any(not isinstance(item, str) for item in raw_return):
        warn_fallback("observed_shape", got=repr(raw_return))
        return None
    return Value((frozenset(date.fromisoformat(item) for item in raw_return), None))


def _routed_classify_observed_payload(
    payload: object, *, raw: bytes
) -> tuple[frozenset[date] | None, str | None]:
    """Rust-first overlay validation — byte-identical decision to the Python law.

    DEC-RUST-CALENDAR-FACADE-FLIP-001. Routes the whole-accept / whole-reject
    overlay decision through the ``brutex_calendar`` crate behind the default-ON
    ``BRUTEX_CALENDAR_RUST`` flag; :func:`_classify_observed_payload` is the LIVE
    parity oracle every degrade / abstain / reject / burn-in-mismatch path serves.
    """
    outcome = routed(
        _LAW_OBSERVED,
        rust_call=lambda crate: crate.parse_observed_sidecar(raw),
        materialize=_materialize_observed,
        oracle=lambda: Value(_classify_observed_payload(payload)),
    )
    return unwrap(outcome)


def _classify_month_drift_python(
    month: date,
    evidence: Sequence[DayEvidence],
    *,
    existing_year_overlay_days: int,
    detected_at: str,
    max_flags_per_month: int = MAX_FLAGS_PER_MONTH,
    max_overlay_days_per_year: int = MAX_OVERLAY_DAYS_PER_YEAR,
    zero_bar_indices: tuple[str, ...] = (),
) -> DriftVerdict:
    """PURE classifier — admission rule + both hard caps, no I/O (the LIVE oracle).

    A candidate is an evidence row with ``zero_bars_all_indices=True``. It is
    ADMITTED iff ``indices_with_month_data == 3`` AND ``clean_success_windows >=``
    :data:`MIN_CLEAN_SUCCESS_WINDOWS` AND ``failed_windows_touching == 0`` (veto).

    Caps run AFTER admission: more than *max_flags_per_month* admitted refuses the
    WHOLE month (``over_month_cap=True, flagged=()`` — never "keep the first 3");
    ``existing_year_overlay_days + admitted`` above *max_overlay_days_per_year*
    refuses too (``over_year_cap=True``). *existing_year_overlay_days* must count
    OTHER months of the year only (this month's own prior entries are being
    replaced — re-detection stays idempotent, Golden Rule #7).
    """
    first_of_month = month.replace(day=1)
    candidates: list[date] = []
    vetoed: list[date] = []
    no_evidence: list[date] = []
    admitted: list[ObservedHoliday] = []
    for row in evidence:
        if not row.zero_bars_all_indices:
            continue
        candidates.append(row.day)
        if row.failed_windows_touching > 0:
            vetoed.append(row.day)
            continue
        if (
            row.indices_with_month_data != _ENGINE_INDEX_COUNT
            or row.clean_success_windows < MIN_CLEAN_SUCCESS_WINDOWS
        ):
            no_evidence.append(row.day)
            continue
        admitted.append(
            ObservedHoliday(
                day=row.day,
                detected_at=detected_at,
                zero_bar_indices=zero_bar_indices,
                clean_success_windows=row.clean_success_windows,
            )
        )
    over_month = len(admitted) > max_flags_per_month
    over_year = (not over_month) and (
        existing_year_overlay_days + len(admitted) > max_overlay_days_per_year
    )
    flagged = () if (over_month or over_year) else tuple(admitted)
    return DriftVerdict(
        month=first_of_month,
        candidates=tuple(candidates),
        flagged=flagged,
        vetoed_failed=tuple(vetoed),
        no_evidence=tuple(no_evidence),
        over_month_cap=over_month,
        over_year_cap=over_year,
    )


#: The ``brutex_calendar.classify_month_drift`` return arity —
#: ``(candidates, flagged, vetoed_failed, no_evidence, over_month_cap, over_year_cap)``.
_CLASSIFY_CRATE_TUPLE_LEN: Final[int] = 6


def _materialize_drift(
    raw_return: object,
    *,
    month: date,
    detected_at: str,
    zero_bar_indices: tuple[str, ...],
) -> LawOutcome[DriftVerdict] | None:
    """Re-hydrate the crate's ``classify_month_drift`` primitive tuple into a
    :class:`DriftVerdict` (re-attaching the decision-inert ``month`` /
    ``detected_at`` / ``zero_bar_indices``).

    The crate echoes day tokens VERBATIM (opaque strings), so each ISO day
    round-trips through :meth:`date.fromisoformat` exactly. A malformed rust
    string reaching ``date.fromisoformat`` (a foreign / broken crate — never the
    real one) raises ``ValueError`` / ``TypeError`` UP into the facade degrade
    set; a wrong SHAPE / leg type is refuse-poison (``None`` + one WARN).
    """
    if not isinstance(raw_return, tuple) or len(raw_return) != _CLASSIFY_CRATE_TUPLE_LEN:
        warn_fallback("classify_shape", got=repr(raw_return))
        return None
    candidates_raw, flagged_raw, vetoed_raw, no_evidence_raw, over_month, over_year = raw_return
    if (
        not isinstance(candidates_raw, list)
        or not isinstance(flagged_raw, list)
        or not isinstance(vetoed_raw, list)
        or not isinstance(no_evidence_raw, list)
        or not isinstance(over_month, bool)
        or not isinstance(over_year, bool)
    ):
        warn_fallback("classify_legs", got=repr(raw_return))
        return None
    flagged = tuple(
        ObservedHoliday(
            day=date.fromisoformat(day_iso),
            detected_at=detected_at,
            zero_bar_indices=zero_bar_indices,
            clean_success_windows=csw,
        )
        for day_iso, csw in flagged_raw
    )
    return Value(
        DriftVerdict(
            month=month.replace(day=1),
            candidates=tuple(date.fromisoformat(d) for d in candidates_raw),
            flagged=flagged,
            vetoed_failed=tuple(date.fromisoformat(d) for d in vetoed_raw),
            no_evidence=tuple(date.fromisoformat(d) for d in no_evidence_raw),
            over_month_cap=over_month,
            over_year_cap=over_year,
        )
    )


def classify_month_drift(
    month: date,
    evidence: Sequence[DayEvidence],
    *,
    existing_year_overlay_days: int,
    detected_at: str,
    max_flags_per_month: int = MAX_FLAGS_PER_MONTH,
    max_overlay_days_per_year: int = MAX_OVERLAY_DAYS_PER_YEAR,
    zero_bar_indices: tuple[str, ...] = (),
) -> DriftVerdict:
    """Rust-first PURE classifier — byte-identical to the Python law on every input.

    DEC-RUST-CALENDAR-FACADE-FLIP-001. Routes the admission → veto →
    month-cap-first / year-cap-mutually-exclusive core through the
    ``brutex_calendar`` crate behind the default-ON ``BRUTEX_CALENDAR_RUST``
    flag; :func:`_classify_month_drift_python` is the LIVE parity oracle every
    degrade / abstain / burn-in-mismatch path serves. Public signature +
    :class:`DriftVerdict` output are byte-unchanged (the sibling-facade law) — the
    crate consumes ``(day_iso, zero_bars_all_indices, indices_with_month_data,
    clean_success_windows, failed_windows_touching)`` primitive rows in
    ``DayEvidence`` field order and echoes day tokens verbatim; the decision-inert
    ``month`` / ``detected_at`` / ``zero_bar_indices`` are re-attached here.
    """
    outcome = routed(
        _LAW_CLASSIFY,
        rust_call=lambda crate: crate.classify_month_drift(
            [
                (
                    row.day.isoformat(),
                    row.zero_bars_all_indices,
                    row.indices_with_month_data,
                    row.clean_success_windows,
                    row.failed_windows_touching,
                )
                for row in evidence
            ],
            existing_year_overlay_days,
            max_flags_per_month,
            max_overlay_days_per_year,
        ),
        materialize=lambda raw_return: _materialize_drift(
            raw_return,
            month=month,
            detected_at=detected_at,
            zero_bar_indices=zero_bar_indices,
        ),
        oracle=lambda: Value(
            _classify_month_drift_python(
                month,
                evidence,
                existing_year_overlay_days=existing_year_overlay_days,
                detected_at=detected_at,
                max_flags_per_month=max_flags_per_month,
                max_overlay_days_per_year=max_overlay_days_per_year,
                zero_bar_indices=zero_bar_indices,
            )
        ),
    )
    return unwrap(outcome)


def _month_bar_dates(lake_root: Path, coord: tuple[str, str, str], month: date) -> set[date]:
    """The set of IST dates carrying 1-minute bars for *coord* in *month*'s file.

    One month-file read per index (``lake_day_glob`` — the IST session never
    crosses a UTC month boundary for a session-filtered engine index). An absent /
    empty month file yields the empty set — data ABSENCE, not holiday evidence
    (the corroboration rule kills any flag it could otherwise support).
    """
    from brutex.calendars.nse import IST
    from brutex.data.lake_day_glob import read_day_month_frame

    exchange, segment, symbol = coord
    base = lake_root / "bars" / exchange / segment / symbol / _EVIDENCE_INTERVAL
    frame = read_day_month_frame(base, month)
    if frame is None or frame.is_empty():
        return set()
    stamps: Iterable[datetime] = frame.get_column("timestamp").to_list()
    return {ts.astimezone(IST).date() for ts in stamps}


def _window_covers(record_start: str, record_end: str, day: date) -> bool:
    """True iff the ledger window ``[start, end)`` covers *day* (end fetch-EXCLUSIVE).

    The ``seed_enclosed_windows`` enclosure convention: ``window_end`` is the
    fetch-exclusive bound, so a window ``[d, d+1)`` covers ``d`` and ``[x, d)``
    does not. A malformed date string never counts as coverage.
    """
    try:
        start = date.fromisoformat(record_start)
        end = date.fromisoformat(record_end)
    except ValueError:
        return False
    return start <= day < end


def _gather_evidence(
    lake_root: Path, month: date, days: Sequence[date]
) -> tuple[list[DayEvidence], tuple[str, ...]]:
    """Build one :class:`DayEvidence` per derived trading day of *month* (I/O half).

    Reads the 3 engine index month files ONCE each + indexes the window ledger
    ONCE. Raises :class:`ObservedSessionsError` when the persisted ledger EXISTED
    but could not be read (``load_failed`` — refuter fold: a corrupt ledger would
    otherwise silently read as "no evidence anywhere"; the reconciler abstains
    LOUDLY instead).
    """
    from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
    from brutex.data.lake_invariants import ENGINE_INDEX_COORDS
    from brutex.data.window_ledger_store import load_window_ledger
    from brutex.data.window_status import FAILED, SUCCESS

    bar_dates_by_coord = {
        coord: _month_bar_dates(lake_root, coord, month) for coord in ENGINE_INDEX_COORDS
    }
    indices_with_month_data = sum(1 for dates in bar_dates_by_coord.values() if dates)
    zero_bar_indices = tuple("/".join(coord) for coord in ENGINE_INDEX_COORDS)
    index_symbols = {coord[2] for coord in ENGINE_INDEX_COORDS}

    ledger = load_window_ledger(lake_root)
    if ledger.load_failed:
        _LOG.error(LEDGER_UNREADABLE_EVENT, lake_root=str(lake_root))
        raise ObservedSessionsError(
            "window ledger exists but is unreadable — no fetch evidence available; "
            "refusing to classify (an unreadable ledger must not silently read as "
            "'no evidence')"
        )
    success_rows: list[tuple[str, str, str]] = []  # (security_id, start, end)
    failed_rows: list[tuple[str, str]] = []  # (start, end)
    for record in ledger.records():
        if (
            record.status == SUCCESS
            and record.market == GROWW_COVERAGE_MARKET
            and record.interval == _EVIDENCE_INTERVAL
            and record.security_id in index_symbols
        ):
            success_rows.append((record.security_id, record.window_start, record.window_end))
        elif record.status == FAILED:
            # Deliberately over-broad (operator-verbatim): ANY FAILED row of ANY
            # market/symbol/interval touching the day vetoes the flag.
            failed_rows.append((record.window_start, record.window_end))

    evidence: list[DayEvidence] = []
    for day in days:
        zero_bars = all(day not in dates for dates in bar_dates_by_coord.values())
        success_per_index = dict.fromkeys(index_symbols, 0)
        for security_id, start, end in success_rows:
            if _window_covers(start, end, day):
                success_per_index[security_id] += 1
        clean_success = min(success_per_index.values())
        failed_touching = sum(1 for start, end in failed_rows if _window_covers(start, end, day))
        evidence.append(
            DayEvidence(
                day=day,
                zero_bars_all_indices=zero_bars,
                indices_with_month_data=indices_with_month_data,
                clean_success_windows=clean_success,
                failed_windows_touching=failed_touching,
            )
        )
    return evidence, zero_bar_indices


def _read_sidecar_entries(path: Path) -> list[dict[str, Any]]:
    """Read the sidecar's raw entry dicts for the merge (tolerant; invalid -> []).

    Rides the SAME bounded, regular-file-only read + widened parse belt as the
    loader (review rounds 4 + 5) — a nesting-bomb / invalid-UTF-8 / oversized /
    non-regular-inode sidecar reads as the empty entry list, never a crash (or
    a FIFO-open hang) of the manual detect run.
    """
    try:
        raw = _bounded_sidecar_read(path)
        if len(raw) > MAX_OVERLAY_BYTES:
            return []
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return []
    if not isinstance(payload, dict) or payload.get("version") != OBSERVED_OVERLAY_VERSION:
        return []
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if _valid_entry_day(entry) is not None]


def _entry_dict(flag: ObservedHoliday, month_key: str) -> dict[str, Any]:
    """Serialize one :class:`ObservedHoliday` into the sidecar entry shape (v1)."""
    return {
        "date": flag.day.isoformat(),
        "detected_at": flag.detected_at,
        "month": month_key,
        "label": flag.label,
        "evidence": {
            "zero_bar_indices": list(flag.zero_bar_indices),
            "clean_success_windows": flag.clean_success_windows,
            "failed_windows_touching": 0,
        },
    }


def _sidecar_bytes(entries: list[dict[str, Any]]) -> str:
    """Deterministic sidecar serialization (sorted entries, sorted keys — Golden Rule #7)."""
    payload = {
        "version": OBSERVED_OVERLAY_VERSION,
        "label": OBSERVED_LABEL,
        "note": (
            "Sessions REMOVED from the derived NSE calendar because the lake OBSERVED "
            "zero bars across all 3 engine indices with clean-SUCCESS ledger evidence "
            "(DEC-CALENDAR-OBSERVED-SESSIONS-001). Observed, NOT the official NSE "
            "circular; an entry never names a holiday. Human-editable escape hatch: "
            "deleting an entry (or this file) restores the plain derived calendar."
        ),
        "entries": sorted(entries, key=lambda entry: str(entry["date"])),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def detect_session_drift(
    lake_root: Path,
    month: date,
    *,
    now: datetime | None = None,
) -> DriftReport:
    """Scan one FULLY-PAST month for observed-holiday drift; reconcile the sidecar.

    The impure driver: gathers evidence (3 month-file reads + one ledger read),
    runs the pure :func:`classify_month_drift`, and reconciles the overlay sidecar
    with a DIFF-BASED write — the sidecar is rewritten whenever the recomputed
    month entry-set differs from the stored one, including the transition to the
    EMPTY set (a stale entry self-heals once real bars appear); a cap refusal is
    strictly no-write.

    Parameters
    ----------
    lake_root
        The data-lake root (bars tree + window ledger + the sidecar live under it).
    month
        Any day inside the month to scan (normalized to day=1). MUST be strictly
        before the current IST month — a forming month's trailing zero-bar days
        are pending pulls, not holidays.
    now
        Injected UTC-aware clock (``None`` -> ``datetime.now(UTC)``); drives both
        the fully-past guard and each NEW entry's ``detected_at`` stamp (an
        UNCHANGED entry keeps its stored ``detected_at`` — byte-idempotent re-run).

    Raises
    ------
    ValueError
        For a current/future month or a month outside the published holiday-list
        coverage (the derived calendar itself is unknown there — the detectors'
        abstain rule, Golden Rule #1).
    ObservedSessionsError
        When the window ledger exists but is unreadable, or the sidecar write
        fails (never a ``LakeWriteHalt`` — this is not the pull path).
    """
    stamp = now if now is not None else datetime.now(UTC)
    first_of_month = month.replace(day=1)
    from brutex.calendars.nse import IST

    today_ist = stamp.astimezone(IST).date()
    if (first_of_month.year, first_of_month.month) >= (today_ist.year, today_ist.month):
        raise ValueError(
            f"month {first_of_month.isoformat()[:7]} is not fully past "
            f"(today IST: {today_ist.isoformat()}) — the reconciler never scans a "
            "forming month (trailing zero-bar days are pending pulls, not holidays)"
        )
    if not is_holiday_data_available(first_of_month):
        raise ValueError(
            f"month {first_of_month.isoformat()[:7]} is outside the published NSE "
            "holiday-list coverage — the derived calendar itself is unknown there, "
            "so drift against it is meaningless (Golden Rule #1: abstain)"
        )

    next_month = (first_of_month + timedelta(days=32)).replace(day=1)
    last_of_month = next_month - timedelta(days=1)
    days = trading_days_between(first_of_month, last_of_month)

    evidence, zero_bar_indices = _gather_evidence(lake_root, first_of_month, days)

    path = overlay_path(lake_root)
    stored_entries = _read_sidecar_entries(path)
    month_key = f"{first_of_month.year:04d}-{first_of_month.month:02d}"
    stored_month = [e for e in stored_entries if str(e["date"]).startswith(month_key)]
    stored_other = [e for e in stored_entries if not str(e["date"]).startswith(month_key)]
    existing_year_days = sum(
        1 for e in stored_other if str(e["date"]).startswith(f"{first_of_month.year:04d}-")
    )

    verdict = classify_month_drift(
        first_of_month,
        evidence,
        existing_year_overlay_days=existing_year_days,
        detected_at=stamp.isoformat(),
        zero_bar_indices=zero_bar_indices,
    )

    for day in verdict.vetoed_failed:
        # WARNING, not info (refuter fold R7): on a mid-backfill lake this is the
        # COMMON terminal outcome — at info the operator would get silence.
        _LOG.warning(FLAG_VETOED_EVENT, day=day.isoformat(), month=month_key)
    for day in verdict.no_evidence:
        _LOG.info(NO_EVIDENCE_EVENT, day=day.isoformat(), month=month_key)
    if verdict.over_month_cap:
        _LOG.error(
            MONTH_CAP_EVENT,
            month=month_key,
            admitted=len(verdict.candidates)
            - len(verdict.vetoed_failed)
            - len(verdict.no_evidence),
            cap=MAX_FLAGS_PER_MONTH,
        )
        return DriftReport(verdict=verdict, overlay_path=str(path), wrote_overlay=False)
    if verdict.over_year_cap:
        _LOG.error(
            YEAR_CAP_EVENT,
            year=first_of_month.year,
            existing=existing_year_days,
            cap=MAX_OVERLAY_DAYS_PER_YEAR,
        )
        return DriftReport(verdict=verdict, overlay_path=str(path), wrote_overlay=False)

    stored_by_day = {str(e["date"]): e for e in stored_month}
    new_month_entries: list[dict[str, Any]] = []
    for flag in verdict.flagged:
        entry = _entry_dict(flag, month_key)
        prior = stored_by_day.get(entry["date"])
        if prior is not None and prior.get("evidence") == entry["evidence"]:
            # Refuter fold R6: unchanged (day, evidence) keeps its original
            # detected_at, so a production re-run is genuinely byte-idempotent.
            entry["detected_at"] = prior["detected_at"]
        new_month_entries.append(entry)
        _LOG.warning(
            DRIFT_DETECTED_EVENT,
            day=flag.day.isoformat(),
            month=month_key,
            zero_bar_indices=list(flag.zero_bar_indices),
            clean_success_windows=flag.clean_success_windows,
            label=flag.label,
        )

    if new_month_entries == stored_month:
        # Diff-based write (refuter fold R2): identical month entry-set -> no write.
        return DriftReport(verdict=verdict, overlay_path=str(path), wrote_overlay=False)

    merged = stored_other + new_month_entries
    text = _sidecar_bytes(merged)
    from brutex.data.atomic_io import atomic_write_simple

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(path, lambda tmp: tmp.write_text(text, encoding="utf-8"))
    except OSError as exc:
        _LOG.error(OVERLAY_WRITE_FAILED_EVENT, path=str(path), error=str(exc))
        raise ObservedSessionsError(f"overlay sidecar write failed: {exc}") from exc
    _LOG.info(
        OVERLAY_WRITTEN_EVENT,
        path=str(path),
        month=month_key,
        entries_total=len(merged),
        entries_month=len(new_month_entries),
    )
    return DriftReport(verdict=verdict, overlay_path=str(path), wrote_overlay=True)
