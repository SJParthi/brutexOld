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

* the last PULL-kind run's :class:`~brutex.dashboard.pull_progress.\
ProgressSnapshot`, sourced from
  :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob.last_pull_state`
  (hostile-review M2 fix: the shared single-flight slot ALSO runs the
  zero-vendor-call SWEEP, whose ``PullProgress.start`` resets the shared
  tracker — the job now RETAINS the last pull's terminal snapshot so a halted
  pull's red outcome survives a later sweep; a sweep's ``done``/``error`` is
  never fed here) — its ``state`` (idle/running/done/error), its ``detail``
  (a failed run records ``"{type(exc).__name__}: {exc}"``; a market-hours
  START deferral — and the pipeline's CAUGHT between-phases market-hours stop,
  whose note becomes the ``done`` finish detail — record the distinct
  ``"deferred — inside NSE market-hours window …"`` message), its ``failed``
  unit count and its ``stalled``/``wedged`` flags;
* the app's last finished-pull VERDICT dict (DEC-VERDICT-BANNER-001 — the
  classified ``clean`` / ``quota_exhausted`` / ``auth_expired`` / … kinds),
  consulted ONLY when its stamped run id matches the retained snapshot's run
  (hostile-review M2: the holder is un-timestamped, so an uncorrelated verdict
  must never colour a different run's outcome), colour-mapped through the ONE
  existing authority :func:`brutex.dashboard.verdict_banner.banner_for`
  (CLAUDE.md §11 COMMON — never a second colour table).

## The outcome vocabulary (stable machine tokens)

``never_run`` · ``running`` · ``running_stalled_suspect`` · ``wedged`` ·
``clean`` · ``completed`` (finished, zero failed units, no classified verdict —
deliberately NOT worded "cleanly") · ``completed_with_failures`` ·
``verdict_<kind>`` (a non-clean classified verdict, e.g.
``verdict_quota_exhausted`` / ``verdict_credentials_missing``) ·
``deferred_market_hours`` (the START-gate deferral AND the pipeline's caught
between-phases market-hours stop) · ``halted_market_hours`` ·
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
* The verdict holder is process-local and un-timestamped, so the PROBE
  correlates it by run id (the holder carries the stamping run's ``pull_id``;
  the retained snapshot carries its own) and passes it here ONLY on a match —
  a route that finishes WITHOUT stamping (e.g. a metadata-only pull) therefore
  reads the honest ``completed``-with-its-own-note, never an older pull's
  verdict (hostile-review M2).  THIS run's own evidence
  (``state``/``detail``/``failed``) still outranks a correlated verdict, and
  the ``completed``/``completed_with_failures`` arms never claim "cleanly".
* Pure + O(1): a handful of string comparisons over already-recorded state —
  no I/O, no clock, no lake read; it changes NO pull behaviour and the
  brute-force ENGINE (NIFTY/BANKNIFTY/SENSEX) is untouched.

## The Rust decision kernel (DEC-RUST-PULL-STATE-001, flip phase)

The ARM decision is served by the ``brutex_pull_state`` Rust kernel as MACHINE
CODES — ``classify_pull_outcome`` + the ``verdict_correlates`` run-id law —
and the kernel is THE ONLY decision path: the Python classify-ladder oracle
was DELETED with the flip, and every kernel fault (wheel absent/stale, a
kernel raise, poisoned codes) raises the typed
:class:`brutex.pull_state_rust.PullStateKernelError` — FAIL-CLOSED, never a
silent degrade and never an unvalidated token on the wire.  Every human
string stays HERE: the code→wording tables below compose the pinned
headlines/details, and the ``@verdict`` / ``@verdict_prepull`` markers hand
the banner fold back to :func:`_from_verdict` so
:func:`brutex.dashboard.verdict_banner.banner_for` stays the ONE colour
authority (CLAUDE.md §11 COMMON).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from brutex import pull_state_rust as _pull_state_rust
from brutex.dashboard.pull_jobs import RetainedPullState
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
#: the real ``start()`` refusal.  The prefix-MATCH law itself lives in the
#: Rust kernel (its literal twin in ``rust/brutex-pull-state`` is pinned by
#: the frozen golden); this constant remains the ONE Python-side spelling the
#: producers + tests couple to.
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


# --- the Rust decision kernel (DEC-RUST-PULL-STATE-001, flip phase) ---------
# The ARM decision is served by the ``brutex_pull_state`` kernel as MACHINE
# CODES — the ONLY decision path since the flip deleted the Python oracle.
# The tables below compose the pinned human wordings Python-side (the
# frontend wire contract stays single-sourced here — headlines are the
# byte-copies of the pre-flip oracle arms, equality-pinned by the frozen
# golden), and the ``@verdict`` markers hand the banner fold back to the
# RETAINED ``_from_verdict`` so ``verdict_banner.banner_for`` stays the ONE
# colour authority (CLAUDE.md §11).  Out-of-contract codes (only a
# stale/corrupt wheel produces them) are REFUSED — the typed
# ``PullStateKernelError`` raise, never an unvalidated token on the wire.

#: headline code → the pinned wording (codes NOT here: ``completed_failures``
#: composes the failed count; ``halt:<Name>`` keys ``_HALT_NAME_OUTCOMES``).
_RUST_HEADLINES: Final[dict[str, str]] = {
    "wedged": "pull WEDGED — restart required",
    "stalled": "pull running — STALLED-SUSPECT (no progress beats)",
    "running": "pull running",
    "deferred_market_hours": "pull deferred — NSE market hours (08:45-15:35 IST)",
    "halted_disk_full": "pull HALTED — disk full",
    "halted_oom": "pull HALTED — out of memory",
    "halted_lake_write": "pull HALTED — lake write halted",
    "failed": "pull FAILED",
    "completed": "pull finished",
    "never_run": "no pull has run in this dashboard process",
}

#: The CLOSED outcome vocabulary the kernel may emit directly (the banner
#: outcomes — ``clean`` / ``verdict_*`` / ``unknown`` — only ever come out of
#: the Python-side ``_from_verdict`` fold, never the kernel).
_RUST_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        OUTCOME_NEVER_RUN,
        OUTCOME_RUNNING,
        OUTCOME_RUNNING_STALLED,
        OUTCOME_WEDGED,
        OUTCOME_COMPLETED,
        OUTCOME_COMPLETED_WITH_FAILURES,
        OUTCOME_DEFERRED_MARKET_HOURS,
        OUTCOME_ERROR,
        "halted_disk_full",
        "halted_oom",
        "halted_lake_write",
    }
    | {outcome for outcome, _tone, _headline in _HALT_NAME_OUTCOMES.values()}
)

#: The heartbeat tone vocabulary (refuse-poison gate for kernel codes).
_RUST_TONES: Final[frozenset[str]] = frozenset({TONE_OK, TONE_WARN, TONE_BAD, TONE_IDLE})


def _refuse_codes(detail: str) -> _pull_state_rust.PullStateKernelError:
    """Build the typed refusal for out-of-contract kernel codes — fail-closed.

    The module-side twin of the facade's refuse-poison gates (the facade
    validates the SHAPE — a 4-tuple of str; the code-level VOCABULARY lives
    here beside the composition tables): only a stale/corrupt wheel emits an
    unrecognisable code, and with the Python oracle deleted the honest
    disposition is the typed raise (``reason=kernel_invalid``), never a
    fabricated wire token (DEC-RUST-PULL-STATE-001, flip phase).
    """
    return _pull_state_rust.PullStateKernelError(
        f"pull-state kernel result for 'classify_pull_outcome' refused "
        f"(reason=kernel_invalid, detail={detail}) — fail-closed: the Python "
        f"oracle was deleted (DEC-RUST-PULL-STATE-001)"
    )


def _compose_from_codes(
    codes: tuple[str, str, str, str],
    *,
    detail: str,
    failed: int,
    verdict: Mapping[str, Any] | None,
) -> LastPullOutcome:
    """Compose the wire outcome from the kernel's decision codes.

    Pure string composition over the code→wording tables — the kernel
    DECIDED, this renders.  Out-of-contract codes (a verdict marker with no
    verdict to fold, an unknown outcome/tone/headline/detail code — each
    structurally impossible from the real drift-pinned kernel) RAISE the
    typed error, never serve an unvalidated token on the wire.

    Raises
    ------
    brutex.pull_state_rust.PullStateKernelError
        On any out-of-vocabulary code (``reason=kernel_invalid``).
    """
    outcome, tone, headline_code, detail_code = codes
    if outcome in ("@verdict", "@verdict_prepull"):
        if verdict is None:
            # A poisoned kernel claiming the verdict arm with no verdict to
            # fold — structurally impossible from the real kernel.
            raise _refuse_codes("verdict marker without a verdict")
        if outcome == "@verdict":
            return _from_verdict(verdict)
        return _from_verdict(verdict, provenance="stamped before any pull started")
    headline: str | None
    if headline_code == "completed_failures":
        headline = f"pull finished — {failed} unit(s) failed/quarantined (will re-pull)"
    elif headline_code.startswith("halt:"):
        mapped = _HALT_NAME_OUTCOMES.get(headline_code.removeprefix("halt:"))
        headline = None if mapped is None else mapped[2]
    else:
        headline = _RUST_HEADLINES.get(headline_code)
    if headline is None or outcome not in _RUST_OUTCOMES or tone not in _RUST_TONES:
        raise _refuse_codes(f"unrecognised outcome codes {codes!r}")
    if detail_code == "input":
        composed_detail = detail
    elif detail_code == "empty":
        composed_detail = ""
    elif detail_code == "completed":
        composed_detail = (
            detail if detail else "completed — no classified verdict recorded for this run"
        )
    else:
        raise _refuse_codes(f"unrecognised detail code {detail_code!r}")
    return LastPullOutcome(outcome=outcome, tone=tone, headline=headline, detail=composed_detail)


def classify_last_pull_outcome(
    *,
    state: str,
    detail: str,
    failed: int,
    stalled: bool,
    wedged: bool,
    verdict: Mapping[str, Any] | None,
) -> LastPullOutcome:
    """Classify the last PULL run's recorded state into the honest outcome.

    Parameters
    ----------
    state, detail, failed, stalled, wedged
        The corresponding :class:`~brutex.dashboard.pull_progress.ProgressSnapshot`
        fields of the job slot's last PULL-kind run — the caller sources them
        from :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob.last_pull_state`
        (SWEEP runs on the shared slot are already filtered out there, and a
        never-ran slot passes ``state="idle"``), so every input here describes
        a pull.
    verdict
        The app's last finished-pull verdict dict
        (:meth:`~brutex.adapters.ingest_common.PullVerdict.to_dict` shape) —
        the caller passes it ONLY when it was stamped by the SAME run the
        snapshot describes (the retained run-id correlation — hostile-review
        M2: an un-timestamped stale verdict must never colour a different
        run's outcome), or on the never-ran slot (where the only writer is
        the pre-pull credentials-refusal gate). ``None`` otherwise.

    Returns
    -------
    LastPullOutcome
        The display-ready outcome.  Every arm is deterministic; nothing is
        fabricated — an unclassifiable input reads an honest
        ``unknown``/``error``, never a green.

    Raises
    ------
    brutex.pull_state_rust.PullStateFlagRemovedError
        When the removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    brutex.pull_state_rust.PullStateKernelError
        Wheel absent/stale, the kernel raised, or it served out-of-contract
        codes — FAIL-CLOSED: the pre-flip Python classify ladder was deleted
        (DEC-RUST-PULL-STATE-001), so an honest outcome cannot be fabricated
        without the kernel.
    """
    codes = _pull_state_rust.rust_classify_pull_outcome(
        state, detail, failed, stalled, wedged, verdict is not None
    )
    return _compose_from_codes(codes, detail=detail, failed=failed, verdict=verdict)


def classify_retained_pull_outcome(
    retained: RetainedPullState | None,
    *,
    verdict: Mapping[str, Any] | None,
    verdict_pull_id: str,
) -> LastPullOutcome:
    """The ONE retained-state → outcome law (the production probe's whole body).

    DEC-OPERATOR-HEALTH-TRUTH-001, hostile-review M2: *retained* is the job
    slot's :meth:`~brutex.dashboard.pull_jobs.BackgroundPullJob.last_pull_state`
    (``None`` = no pull ever ran/deferred on the slot); *verdict* +
    *verdict_pull_id* are the app's last-pull-verdict holder pair. The verdict
    is consulted ONLY when it was stamped by the SAME run the retained snapshot
    describes (id equality over a NON-empty retained id — a run-less
    deferral/refusal record carries ``""`` and correlates with nothing), except
    on the never-ran slot, where the only legitimate writer is the pre-pull
    credentials-refusal gate.  Pure + O(1); the app probe and the tests drive
    THIS function, so the correlation law has exactly one home (CLAUDE.md §11) —
    served by the kernel's ``verdict_correlates`` since the flip
    (DEC-RUST-PULL-STATE-001; a kernel fault raises
    :class:`~brutex.pull_state_rust.PullStateKernelError`, fail-closed).
    """
    if retained is None:
        return classify_last_pull_outcome(
            state="idle", detail="", failed=0, stalled=False, wedged=False, verdict=verdict
        )
    snap = retained.snapshot
    correlated = (
        verdict
        if _pull_state_rust.rust_verdict_correlates(retained.pull_id, verdict_pull_id)
        else None
    )
    return classify_last_pull_outcome(
        state=snap.state,
        detail=snap.detail,
        failed=snap.failed,
        stalled=snap.stalled,
        wedged=snap.wedged,
        verdict=correlated,
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
    "classify_retained_pull_outcome",
]
