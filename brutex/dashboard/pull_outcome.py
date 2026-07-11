"""Honest LAST-PULL OUTCOME projection (DEC-OPERATOR-HEALTH-TRUTH-001, MIGRATION.md #1747).

The operator page's heartbeat pull slot renders the ``/api/v1/pull/rate``
``flow_state``.  Its ``done`` state is TRUE but WEAK evidence: per
:mod:`brutex.dashboard.pull_rate_view` it means only "no pull running + the last
rate tick is still fresh" — it carries NO completion verdict.  The pre-fix
frontend rendered it as a green "last pull completed cleanly", so a pull HALTED
by a full disk (``LakeWriteHalt``), the market-hours guard (``MarketHoursHalt``),
a quota stop or ANY other pull-wide failure showed the SAME green as a genuinely
clean completion — an honesty-of-display violation (Golden Rule #1).

This module is the pure read-side classifier that closes the gap.  It joins the
truth sources the dashboard ALREADY records (nothing new is invented):

* the :class:`~brutex.dashboard.pull_progress.ProgressSnapshot` of the app's
  single-flight :class:`~brutex.dashboard.pull_jobs.BackgroundPullJob` — its
  ``state`` (idle/running/done/error), its ``detail`` (a failed run records
  ``"{type(exc).__name__}: {exc}"``; a market-hours START deferral records the
  distinct ``"deferred — inside NSE market-hours window …"`` message), its
  ``failed`` unit count and its ``stalled``/``wedged`` flags;
* the job's retained :attr:`~brutex.dashboard.pull_jobs.BackgroundPullJob.\
last_job_kind` — the shared single-flight slot ALSO runs the zero-vendor-call
  SWEEP, whose ``done`` must never be claimed as a pull outcome;
* the app's last finished-pull VERDICT dict (DEC-VERDICT-BANNER-001 — the
  classified ``clean`` / ``quota_exhausted`` / ``auth_expired`` / … kinds),
  colour-mapped through the ONE existing authority
  :func:`brutex.dashboard.verdict_banner.banner_for` (CLAUDE.md §11 COMMON —
  never a second colour table).

## The outcome vocabulary (stable machine tokens)

``never_run`` · ``running`` · ``running_stalled_suspect`` · ``wedged`` ·
``clean`` · ``completed`` (finished, zero failed units, no classified verdict —
deliberately NOT worded "cleanly") · ``completed_with_failures`` ·
``verdict_<kind>`` (a non-clean classified verdict, e.g.
``verdict_quota_exhausted`` / ``verdict_credentials_missing``) ·
``deferred_market_hours`` (start-gate deferral) · ``halted_market_hours`` ·
``halted_disk_full`` · ``halted_oom`` · ``halted_lake_write`` ·
``halted_quota`` · ``halted_pool_broken`` · ``halted_vendor_down`` ·
``halted_mass_empty`` · ``halted_auth`` · ``refused_busy`` · ``error`` ·
``unknown``.  ``tone`` ∈ ``ok``/``warn``/``bad``/``idle`` — exactly the
operator-page heartbeat's ``setSlot`` colour vocabulary, so the frontend maps
it 1:1 with zero re-derivation.

## Honest envelope (Golden Rule #10)

* Covers pulls run by THIS dashboard process's job slot.  A CLI ``brutex
  fetch`` pull publishes rate numbers over the cross-process bridge but NO
  outcome — its halt is alerted on Telegram and visible in the CLI exit code,
  not here (a named limit, never a fabricated verdict).
* The verdict holder is process-local and un-timestamped: it is stamped by the
  pull routes' own workers, so on the normal path it belongs to the same run
  the snapshot describes — but a route that finishes WITHOUT stamping (e.g. a
  metadata-only pull) leaves an older pull's verdict in place.  The classifier
  therefore prefers THIS run's own evidence (``state``/``detail``/``failed``)
  and consults the verdict only to REFINE a finished run's colour, with the
  ``completed``/``completed_with_failures`` arms never claiming "cleanly".
* Pure + O(1): a handful of string comparisons over already-recorded state —
  no I/O, no clock, no lake read; it changes NO pull behaviour and the
  brute-force ENGINE (NIFTY/BANKNIFTY/SENSEX) is untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from brutex.dashboard.pull_jobs import JOB_KIND_PULL
from brutex.dashboard.verdict_banner import banner_for

#: Tone tokens — EXACTLY the operator-page heartbeat ``setSlot`` vocabulary
#: (green / amber / red / neutral-dark).  The frontend maps them 1:1.
TONE_OK: Final[str] = "ok"
TONE_WARN: Final[str] = "warn"
TONE_BAD: Final[str] = "bad"
TONE_IDLE: Final[str] = "idle"

#: Outcome tokens (the stable machine vocabulary — see the module docstring).
OUTCOME_NEVER_RUN: Final[str] = "never_run"
OUTCOME_RUNNING: Final[str] = "running"
OUTCOME_RUNNING_STALLED: Final[str] = "running_stalled_suspect"
OUTCOME_WEDGED: Final[str] = "wedged"
OUTCOME_CLEAN: Final[str] = "clean"
OUTCOME_COMPLETED: Final[str] = "completed"
OUTCOME_COMPLETED_WITH_FAILURES: Final[str] = "completed_with_failures"
OUTCOME_DEFERRED_MARKET_HOURS: Final[str] = "deferred_market_hours"
OUTCOME_ERROR: Final[str] = "error"
OUTCOME_UNKNOWN: Final[str] = "unknown"

#: The market-hours START-gate deferral detail prefix — the EXACT message
#: :meth:`brutex.dashboard.pull_jobs.BackgroundPullJob.start` records
#: (DEC-GROWW-MARKET-HOURS-GUARD-001); drift-pinned by the unit suite against
#: the real ``start()`` refusal.
MARKET_HOURS_DEFERRAL_PREFIX: Final[str] = "deferred — inside NSE market-hours window"

#: Failed-run detail classification: the job worker records a failed run as
#: ``"{type(exc).__name__}: {exc}"`` (:meth:`BackgroundPullJob._run`), so the
#: leading exception NAME token is recorded evidence, not a guess.  Keys are
#: drift-pinned by the unit suite against the REAL exception classes named in
#: :func:`brutex.dashboard.pull_jobs.is_pull_halt`.  ``LakeWriteHalt`` is
#: handled separately (its ``reason`` splits disk-full vs OOM).
_HALT_NAME_OUTCOMES: Final[dict[str, tuple[str, str, str]]] = {
    # A mid-run market-hours halt is a BY-DESIGN graceful stop (the ledger
    # stays resumable; the next nightly re-pulls) — amber, never red.
    "MarketHoursHalt": (
        "halted_market_hours",
        TONE_WARN,
        "pull halted — NSE market hours (resumes next off-hours run)",
    ),
    "DailyQuotaExceeded": ("halted_quota", TONE_BAD, "pull HALTED — daily quota exhausted"),
    "QuotaInvariantError": ("halted_quota", TONE_BAD, "pull HALTED — quota state corrupt"),
    "GreeksEnrichPoolBroken": (
        "halted_pool_broken",
        TONE_BAD,
        "pull HALTED — Greeks enrichment pool broken",
    ),
    "MassEmptySettleHalt": (
        "halted_mass_empty",
        TONE_BAD,
        "pull HALTED — mass-empty circuit breaker (triage before re-pulling)",
    ),
    "VendorDownError": ("halted_vendor_down", TONE_BAD, "pull HALTED — vendor down / error storm"),
    "GrowwCredentialDeadError": (
        "halted_auth",
        TONE_BAD,
        "pull HALTED — Groww credential dead (re-mint required)",
    ),
    "PullAlreadyRunningError": (
        "refused_busy",
        TONE_WARN,
        "pull refused — another pull is already running",
    ),
}

#: Verdict-banner colour → heartbeat tone.  The COLOURS come verbatim from the
#: ONE authority :func:`brutex.dashboard.verdict_banner.banner_for`
#: (red/amber/grey/green — DEC-VERDICT-BANNER-001); this maps them onto the
#: heartbeat's narrower ok/warn/bad palette.  ``grey`` (quarantined symbols /
#: known gap) folds to ``warn`` — visually degraded, NEVER green.
_COLOUR_TONES: Final[dict[str, str]] = {
    "green": TONE_OK,
    "amber": TONE_WARN,
    "grey": TONE_WARN,
    "red": TONE_BAD,
}


@dataclass(frozen=True, slots=True)
class LastPullOutcome:
    """The display-ready last-pull outcome (immutable, JSON-friendly).

    Attributes
    ----------
    outcome
        A stable machine token from the module vocabulary.
    tone
        ``ok`` / ``warn`` / ``bad`` / ``idle`` — the heartbeat colour.
    headline
        Operator-facing one-liner (rendered via ``textContent`` only).
    detail
        Supporting evidence line — the recorded failure detail / the verdict's
        remediation ``action`` / an honest provenance note.  May be ``""``.
    """

    outcome: str
    tone: str
    headline: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """Return the additive ``last_pull`` wire block (4 stable keys)."""
        return {
            "outcome": self.outcome,
            "tone": self.tone,
            "headline": self.headline,
            "detail": self.detail,
        }


def _from_verdict(verdict: Mapping[str, Any], *, provenance: str = "") -> LastPullOutcome:
    """Fold a classified pull VERDICT dict into an outcome (colour via ``banner_for``).

    Delegates the kind→colour decision to the ONE existing authority
    (:func:`~brutex.dashboard.verdict_banner.banner_for` — CLAUDE.md §11); an
    unknown / malformed verdict shape degrades to an honest ``unknown`` WARN,
    never a raise and never a fabricated green.
    """
    try:
        banner = banner_for(verdict)
    except (KeyError, TypeError, ValueError) as exc:
        return LastPullOutcome(
            outcome=OUTCOME_UNKNOWN,
            tone=TONE_WARN,
            headline="pull verdict unreadable",
            detail=f"unrecognised verdict shape: {exc}",
        )
    outcome = OUTCOME_CLEAN if banner.kind == "clean" else f"verdict_{banner.kind}"
    detail = banner.action
    if provenance:
        detail = f"{detail} [{provenance}]" if detail else provenance
    return LastPullOutcome(
        outcome=outcome,
        tone=_COLOUR_TONES[banner.colour],
        headline=banner.headline,
        detail=detail,
    )


def _from_error_detail(detail: str) -> LastPullOutcome:
    """Classify a FAILED run's recorded ``detail`` string (recorded evidence only)."""
    if detail.startswith(MARKET_HOURS_DEFERRAL_PREFIX):
        # The START gate refused to begin a vendor pull inside the NSE window
        # (DEC-GROWW-MARKET-HOURS-GUARD-001) — a deliberate deferral, not a failure.
        return LastPullOutcome(
            outcome=OUTCOME_DEFERRED_MARKET_HOURS,
            tone=TONE_WARN,
            headline="pull deferred — NSE market hours (08:45-15:35 IST)",
            detail=detail,
        )
    name = detail.split(":", 1)[0]
    if name == "LakeWriteHalt":
        # ``LakeWriteHalt`` messages read "lake write halted: <reason>" with
        # reason ∈ {disk_full, out_of_memory} (brutex/data/errors.py — pinned
        # by the unit suite against the real class).
        if "disk_full" in detail:
            return LastPullOutcome(
                outcome="halted_disk_full",
                tone=TONE_BAD,
                headline="pull HALTED — disk full",
                detail=detail,
            )
        if "out_of_memory" in detail:
            return LastPullOutcome(
                outcome="halted_oom",
                tone=TONE_BAD,
                headline="pull HALTED — out of memory",
                detail=detail,
            )
        return LastPullOutcome(
            outcome="halted_lake_write",
            tone=TONE_BAD,
            headline="pull HALTED — lake write halted",
            detail=detail,
        )
    mapped = _HALT_NAME_OUTCOMES.get(name)
    if mapped is not None:
        outcome, tone, headline = mapped
        return LastPullOutcome(outcome=outcome, tone=tone, headline=headline, detail=detail)
    return LastPullOutcome(
        outcome=OUTCOME_ERROR,
        tone=TONE_BAD,
        headline="pull FAILED",
        detail=detail,
    )


def classify_last_pull_outcome(
    *,
    state: str,
    detail: str,
    failed: int,
    stalled: bool,
    wedged: bool,
    job_kind: str,
    verdict: Mapping[str, Any] | None,
) -> LastPullOutcome:
    """Classify the job slot's recorded state into the honest last-pull outcome.

    Parameters
    ----------
    state, detail, failed, stalled, wedged
        The corresponding :class:`~brutex.dashboard.pull_progress.ProgressSnapshot`
        fields of the app's single-flight pull job.
    job_kind
        The job slot's :attr:`~brutex.dashboard.pull_jobs.BackgroundPullJob.\
last_job_kind` — a retained ``done``/``error`` from a SWEEP run must never be
        claimed as a pull outcome (the verdict holder, stamped only by pull
        routes, is then the only honest source).
    verdict
        The app's last finished-pull verdict dict
        (:meth:`~brutex.adapters.ingest_common.PullVerdict.to_dict` shape), or
        ``None`` when no pull has stamped one.

    Returns
    -------
    LastPullOutcome
        The display-ready outcome.  Every arm is deterministic; nothing is
        fabricated — an unclassifiable input degrades to an honest
        ``unknown``/``error``, never a green.
    """
    if state == "running":
        if job_kind != JOB_KIND_PULL:
            # A SWEEP is running on the shared slot — not a pull outcome at all;
            # the pull slot honestly reports the LAST pull's verdict if one exists.
            if verdict is not None:
                return _from_verdict(verdict, provenance="from the last completed pull's verdict")
            return LastPullOutcome(
                outcome=OUTCOME_UNKNOWN,
                tone=TONE_IDLE,
                headline="no pull outcome recorded",
                detail="a non-pull job (sweep) is running on this slot",
            )
        if wedged:
            return LastPullOutcome(
                outcome=OUTCOME_WEDGED,
                tone=TONE_BAD,
                headline="pull WEDGED — restart required",
                detail=detail,
            )
        if stalled:
            return LastPullOutcome(
                outcome=OUTCOME_RUNNING_STALLED,
                tone=TONE_WARN,
                headline="pull running — STALLED-SUSPECT (no progress beats)",
                detail=detail,
            )
        return LastPullOutcome(
            outcome=OUTCOME_RUNNING, tone=TONE_OK, headline="pull running", detail=detail
        )
    if state == "error":
        if job_kind != JOB_KIND_PULL:
            # A failed SWEEP must not masquerade as a failed PULL.
            return LastPullOutcome(
                outcome=OUTCOME_UNKNOWN,
                tone=TONE_IDLE,
                headline="no pull outcome recorded",
                detail="the last job on this slot was a sweep (its failure is not a pull outcome)",
            )
        return _from_error_detail(detail)
    if state == "done":
        if job_kind != JOB_KIND_PULL:
            # The retained ``done`` belongs to a SWEEP run; the only honest pull
            # evidence left is the verdict holder (stamped by pull routes only).
            if verdict is not None:
                return _from_verdict(verdict, provenance="from the last completed pull's verdict")
            return LastPullOutcome(
                outcome=OUTCOME_UNKNOWN,
                tone=TONE_IDLE,
                headline="no pull outcome recorded",
                detail="the last job on this slot was a sweep",
            )
        if failed > 0:
            # THIS run's own quarantine tally outranks the (possibly older)
            # verdict holder — never green over recorded failures.
            return LastPullOutcome(
                outcome=OUTCOME_COMPLETED_WITH_FAILURES,
                tone=TONE_WARN,
                headline=f"pull finished — {failed} unit(s) failed/quarantined (will re-pull)",
                detail=detail,
            )
        if verdict is not None:
            return _from_verdict(verdict)
        return LastPullOutcome(
            outcome=OUTCOME_COMPLETED,
            tone=TONE_OK,
            headline="pull finished",
            detail="completed — no classified verdict recorded for this run",
        )
    # ``idle`` — nothing has run on the slot this process lifetime.  A stamped
    # verdict can still exist (the pre-pull credentials REFUSAL is stamped at
    # the route gate BEFORE any start — DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001) —
    # surface it honestly instead of a blank neutral.
    if verdict is not None:
        return _from_verdict(verdict, provenance="stamped before any pull started")
    return LastPullOutcome(
        outcome=OUTCOME_NEVER_RUN,
        tone=TONE_IDLE,
        headline="no pull has run in this dashboard process",
        detail="",
    )


__all__ = [
    "MARKET_HOURS_DEFERRAL_PREFIX",
    "OUTCOME_CLEAN",
    "OUTCOME_COMPLETED",
    "OUTCOME_COMPLETED_WITH_FAILURES",
    "OUTCOME_DEFERRED_MARKET_HOURS",
    "OUTCOME_ERROR",
    "OUTCOME_NEVER_RUN",
    "OUTCOME_RUNNING",
    "OUTCOME_RUNNING_STALLED",
    "OUTCOME_UNKNOWN",
    "OUTCOME_WEDGED",
    "TONE_BAD",
    "TONE_IDLE",
    "TONE_OK",
    "TONE_WARN",
    "LastPullOutcome",
    "classify_last_pull_outcome",
]
