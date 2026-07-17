"""Deterministic epoch-timestamp interpretation cross-check — the VERDICT.

DEC-GROWW-EPOCH-VERDICT-001 — closes the ``epoch-timestamp-verdict`` debt of
DEC-INGEST-BOUNDARY-INTEGRITY-001 (b). The parser's DEFENSIVE epoch branch
(:func:`brutex.adapters.groww.parser.parse_timestamp_ist`) assumes a numeric
candle timestamp is **Unix-UTC seconds**; the vendor's epoch semantics are
UNVERIFIED (no live probe transcript), and if Groww ever sent **IST-naive**
epoch seconds instead, every bar would silently shift by +05:30.

A live vendor probe is the ONLY thing that can VERIFY the semantics (see the
runbook ``docs/runbooks/groww-epoch-timestamp-probe.md``). What CAN be done
deterministically offline is a **session-plausibility cross-check**: a genuine
intraday candle's wall-clock time must fall inside its date's real NSE trading
session (via the calendar's :func:`~brutex.calendars.nse.get_session_checked`
— the ONE session source of truth, Muhurat/special-weekend aware). The two
candidate interpretations of one epoch value differ by exactly +05:30 of wall
clock, so most in-session instants are consistent with only ONE of them:

* interpretation **unix-utc**: wall IST = ``fromtimestamp(epoch, UTC) + 05:30``
  (what the parser stores today);
* interpretation **ist-naive**: wall IST = ``fromtimestamp(epoch, UTC)`` read
  directly as an IST wall clock (the feared mis-send).

Classifying every epoch sample of a payload against both interpretations
yields an aggregate :class:`EpochVerdict` — evidence FOR the current
assumption (``unix_utc_consistent``), evidence AGAINST it
(``ist_naive_consistent`` — the loud case), ``contradictory`` (samples point
both ways), or ``no_evidence`` (every sample ambiguous / out-of-session /
out-of-calendar-coverage / invalid).

HONESTY (Golden Rule #1): this cross-check is deterministic **evidence**, NOT
vendor verification — an in-session wall clock under one interpretation makes
that interpretation *plausible*, it does not prove the vendor meant it. The
UNVERIFIED stamp on the epoch semantics stands until a live probe transcript
lands (the runbook). Parsing behaviour is UNCHANGED — this module is
observe-only and never influences the stored bar.

Out-of-coverage calendar dates ABSTAIN (never fabricate a session verdict) —
SILENTLY, via the non-warning
:func:`~brutex.calendars.nse.is_holiday_data_available` pre-check (the
round-1 review log-storm fold): routing such a date through the strict
:func:`~brutex.calendars.nse.get_session_checked` emits one
``calendar.holiday_data_unavailable`` WARNING per call, and with TWO
interpretation checks per sample × :data:`MAX_VERDICT_SAMPLES` samples per
payload × every window of a pull, an out-of-coverage epoch payload (e.g. a
2027 pull after a vendor epoch flip — the exact scenario this module
diagnoses) would have stormed up to 2,048 undeduplicated WARNs PER WINDOW.
The ONE aggregate ``groww.parse.epoch_timestamp_verdict`` WARN (the parser)
stays the loud surface. The pre-check deliberately abstains on
out-of-coverage WEEKENDS too — their no-session verdict is data-complete,
but an abstain is strictly conservative (an out-of-coverage date can never
read IN-session under the strict calendar, so no directional evidence is
ever produced there; at most an ``out_of_session`` tally becomes
``abstained``).

O(1) per sample (two calendar dict lookups); the aggregate is O(min(samples,
:data:`MAX_VERDICT_SAMPLES`)) — bounded, deterministic (first-N in input
order).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Final

from brutex.calendars.nse import (
    IST_UTC_OFFSET,
    get_session_checked,
    is_holiday_data_available,
)

#: The fixed IST offset (+05:30, no DST) between the two candidate
#: interpretations of one epoch value — the ONE canonical
#: :data:`brutex.calendars.nse.IST_UTC_OFFSET` root
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001).
_IST_OFFSET: Final = IST_UTC_OFFSET

#: Classification cap — at most this many samples of a payload are classified
#: (first-N in input order, deterministic). Bounds the calendar-lookup work
#: (and the calendar's own out-of-coverage WARNINGs) for a pathological
#: all-epoch multi-day payload; :attr:`EpochVerdict.sampled` reports the
#: actually-classified count honestly next to :attr:`EpochVerdict.total`.
MAX_VERDICT_SAMPLES: Final = 1024


class EpochSampleEvidence(Enum):
    """What ONE epoch sample says about the two candidate interpretations."""

    #: Only the Unix-UTC interpretation lands in a real trading session.
    SUPPORTS_UNIX_UTC = "supports_unix_utc"
    #: Only the IST-naive interpretation lands in a real trading session —
    #: evidence the parser's assumption is WRONG for this sample.
    SUPPORTS_IST_NAIVE = "supports_ist_naive"
    #: BOTH interpretations land in-session (the ~45-minute daily overlap
    #: window) — no discriminating power.
    AMBIGUOUS = "ambiguous"
    #: NEITHER interpretation lands in-session — a genuinely odd timestamp
    #: (or non-session data); no discriminating power.
    OUT_OF_SESSION = "out_of_session"
    #: A calendar date outside holiday coverage — the session verdict would
    #: depend on missing data, so the sample honestly abstains.
    ABSTAINED = "abstained"
    #: Not classifiable at all: bool, non-numeric, non-finite, or an epoch
    #: whose datetime conversion overflows.
    INVALID = "invalid"


class EpochVerdictKind(Enum):
    """The aggregate verdict over a payload's epoch samples."""

    #: Every directional sample supports the current Unix-UTC assumption.
    UNIX_UTC_CONSISTENT = "unix_utc_consistent"
    #: Every directional sample supports the IST-naive interpretation — the
    #: parser's assumption is likely WRONG; escalate via the runbook.
    IST_NAIVE_CONSISTENT = "ist_naive_consistent"
    #: Directional samples point BOTH ways — the payload is internally
    #: inconsistent; escalate via the runbook.
    CONTRADICTORY = "contradictory"
    #: No directional sample at all (all ambiguous / out-of-session /
    #: abstained / invalid).
    NO_EVIDENCE = "no_evidence"


@dataclass(frozen=True, slots=True)
class EpochVerdict:
    """Immutable aggregate of :func:`classify_epoch_timestamps`."""

    verdict: EpochVerdictKind
    supports_unix_utc: int
    supports_ist_naive: int
    ambiguous: int
    out_of_session: int
    abstained: int
    invalid: int
    #: Samples handed in (may exceed ``sampled`` when capped).
    total: int
    #: Samples actually classified (``min(total, max_samples)``).
    sampled: int


def _wall_in_session(wall_ist: datetime) -> bool | None:
    """Is the naive IST wall-clock instant inside its date's trading session?

    ``None`` = ABSTAIN — the date lies outside the covered holiday span,
    pre-checked with the NON-warning
    :func:`~brutex.calendars.nse.is_holiday_data_available` (the round-1
    review log-storm fold: routing an out-of-coverage date through the strict
    :func:`~brutex.calendars.nse.get_session_checked` fires one
    ``calendar.holiday_data_unavailable`` WARNING per call — up to 2 × 1024
    per payload, per window). The abstain covers out-of-coverage WEEKENDS too
    (data-complete but deliberately abstained — see the module docstring; an
    out-of-coverage date can never read in-session, so no directional
    evidence is lost). Within coverage, the strict calendar never raises, so
    the session resolves directly. Boundary instants (exactly at open /
    close) count as in-session — inclusive on BOTH ends, deliberately WIDER
    than the bar-label half-open convention of
    :func:`brutex.data.session.filter_to_trading_session` (``open <= t <
    close``): a boundary instant then reads in-session under both candidate
    interpretations more often, i.e. it can only ADD ambiguity, never
    manufacture directional evidence from a knife-edge (conservative for an
    evidence check).
    """
    if not is_holiday_data_available(wall_ist.date()):
        return None
    session = get_session_checked(wall_ist.date())
    if session is None:
        return False
    return session.open_time_ist <= wall_ist.time() <= session.close_time_ist


def classify_epoch_sample(epoch: object) -> EpochSampleEvidence:
    """Classify ONE raw epoch timestamp sample against both interpretations.

    Accepts the raw row value (``object``) so callers can feed unvalidated
    vendor data: bools (an ``int`` subclass), non-numerics, non-finite floats
    and datetime-overflow epochs all classify as
    :attr:`EpochSampleEvidence.INVALID` — never raise. O(1).
    """
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return EpochSampleEvidence.INVALID
    value = float(epoch)
    if not math.isfinite(value):
        return EpochSampleEvidence.INVALID
    try:
        base = datetime.fromtimestamp(value, tz=UTC)
        # unix-utc interpretation: the stored-UTC instant displayed in IST.
        wall_unix_utc = (base + _IST_OFFSET).replace(tzinfo=None)
        # ist-naive interpretation: the epoch counted seconds of IST wall
        # clock since 1970-01-01T00:00:00 (wall = base read directly).
        wall_ist_naive = base.replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return EpochSampleEvidence.INVALID
    unix_utc_in = _wall_in_session(wall_unix_utc)
    ist_naive_in = _wall_in_session(wall_ist_naive)
    if unix_utc_in is None or ist_naive_in is None:
        return EpochSampleEvidence.ABSTAINED
    if unix_utc_in and ist_naive_in:
        return EpochSampleEvidence.AMBIGUOUS
    if unix_utc_in:
        return EpochSampleEvidence.SUPPORTS_UNIX_UTC
    if ist_naive_in:
        return EpochSampleEvidence.SUPPORTS_IST_NAIVE
    return EpochSampleEvidence.OUT_OF_SESSION


def classify_epoch_timestamps(
    samples: list[object] | tuple[object, ...],
    *,
    max_samples: int = MAX_VERDICT_SAMPLES,
) -> EpochVerdict:
    """Aggregate a payload's epoch samples into one :class:`EpochVerdict`.

    Deterministic: classifies the FIRST ``min(len(samples), max_samples)``
    samples in input order and derives the verdict from the directional
    counts only — evidence FOR the assumption, AGAINST it, both
    (``contradictory``) or neither (``no_evidence``). Never raises on vendor
    junk (invalid samples are counted, not fatal).

    Raises
    ------
    ValueError
        If ``max_samples`` is not positive (a zero/negative cap would
        silently classify nothing — refused loudly instead).
    """
    if max_samples < 1:
        raise ValueError(f"max_samples must be >= 1, got {max_samples}")
    counts: dict[EpochSampleEvidence, int] = dict.fromkeys(EpochSampleEvidence, 0)
    classified = samples[:max_samples]
    for sample in classified:
        counts[classify_epoch_sample(sample)] += 1
    supports_unix_utc = counts[EpochSampleEvidence.SUPPORTS_UNIX_UTC]
    supports_ist_naive = counts[EpochSampleEvidence.SUPPORTS_IST_NAIVE]
    if supports_unix_utc and supports_ist_naive:
        verdict = EpochVerdictKind.CONTRADICTORY
    elif supports_unix_utc:
        verdict = EpochVerdictKind.UNIX_UTC_CONSISTENT
    elif supports_ist_naive:
        verdict = EpochVerdictKind.IST_NAIVE_CONSISTENT
    else:
        verdict = EpochVerdictKind.NO_EVIDENCE
    return EpochVerdict(
        verdict=verdict,
        supports_unix_utc=supports_unix_utc,
        supports_ist_naive=supports_ist_naive,
        ambiguous=counts[EpochSampleEvidence.AMBIGUOUS],
        out_of_session=counts[EpochSampleEvidence.OUT_OF_SESSION],
        abstained=counts[EpochSampleEvidence.ABSTAINED],
        invalid=counts[EpochSampleEvidence.INVALID],
        total=len(samples),
        sampled=len(classified),
    )


__all__ = [
    "MAX_VERDICT_SAMPLES",
    "EpochSampleEvidence",
    "EpochVerdict",
    "EpochVerdictKind",
    "classify_epoch_sample",
    "classify_epoch_timestamps",
]
