"""Actionable verdict for a finished spot pull — never let a stuck pull sit silent.

A bulk spot pull (:func:`brutex.dashboard.pull_controller.pull_all_spot`) already
records the raw outcome (``auth_aborted`` / ``still_failed_windows`` /
``failed_pairs`` / ``no_data`` / ``completed``), but those numbers alone do not
tell the operator **what to DO**. The most damaging case is silent: the Dhan
24-hour token expires mid-pull (``DH-906 Invalid Token``), the pull aborts
(DEC-DHAN-AUTH-FAILFAST-RENEW-001), the windows are correctly left un-marked so
they re-fetch next run (DEC-FETCH-COVERAGE-001) — but a retry can NEVER succeed
on a dead token, so without a loud, actionable signal the operator re-pulls into
the same wall forever (the "again and again N failed windows" report bug, where
a missing prior-day window then produced a wrong %CHNG).

:func:`classify_pull` turns the raw tallies into ONE prioritised, human-readable
:class:`PullVerdict` with an explicit next action. It is a pure, deterministic
function (no I/O, no clock) so it is trivially testable and reused by BOTH the
automatic surface (the structured ``universe.pull_all.done`` /
``universe.pull_all.action_required`` logs + any alerter) and the manual surface
(the ``/api/v1/universe/pull-all`` JSON the dashboard renders) — CLAUDE.md §11
COMMON. **Honest envelope (Golden Rule #10):** a verdict only *classifies +
advises*; it cannot itself fetch data or renew a token. ``AUTH_EXPIRED`` means
"a human must regenerate the token, then re-pull"; ``TRANSIENT_RESIDUAL`` means
"re-pull / let auto-retry finish"; ``KNOWN_GAP_ONLY`` means "Dhan genuinely has
no data for these (reference-only) symbols — expected, not an error".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

#: The mutually-exclusive, priority-ordered outcomes of a finished pull.
PullVerdictKind = Literal[
    "auth_expired",
    "quota_exhausted",
    "transient_residual",
    "known_gap_only",
    "clean",
]

#: The operator-facing headline + next action for each kind (DEC-PULL-VERDICT-001).
#: Kept as data (not f-strings) so the message is deterministic + test-pinnable;
#: dynamic counts are carried as structured fields on :class:`PullVerdict`.
_HEADLINE: Final[dict[str, str]] = {
    "auth_expired": "🔑 Dhan token expired mid-pull — the pull aborted.",
    "quota_exhausted": "📊 Daily Dhan quota reached — auto-resume paused for today.",
    "transient_residual": "⏳ Some windows are still pending after retries.",
    "known_gap_only": "ℹ️ No new bars — these symbols have no intraday on Dhan.",
    "clean": "✅ Pull complete.",
}
_ACTION: Final[dict[str, str]] = {
    "auth_expired": (
        "Regenerate the 24-hour token at web.dhan.co, then re-pull. "
        "A retry can NEVER succeed on a dead token — only a fresh token can."
    ),
    "quota_exhausted": (
        "The 100,000/day Dhan quota is spent — the auto-resume paused itself "
        "(it will NOT keep burning a dead quota). Re-pull after the IST-midnight "
        "reset; already-pulled windows are skipped, so only the gaps re-fetch."
    ),
    "transient_residual": (
        "Re-pull to finish — already-pulled windows are skipped and only the "
        "still-pending windows are re-fetched (auto-retry mops up transient 429s)."
    ),
    "known_gap_only": (
        "No action needed — these are reference-only symbols Dhan does not serve "
        "intraday (e.g. broad-market indices); they are not brute-force inputs."
    ),
    "clean": "No action needed.",
}


@dataclass(frozen=True, slots=True)
class PullVerdict:
    """A finished pull's single, prioritised, actionable verdict.

    Immutable + JSON-serialisable. ``kind`` is the worst (highest-priority)
    condition observed; ``pending_windows`` is the count of work that still has
    to succeed (the still-failed windows + the quarantined pairs) so the
    operator knows the pull is NOT done. ``auth_cause`` carries the originating
    Dhan error string ONLY for ``auth_expired`` (else ``None``).
    """

    kind: PullVerdictKind
    headline: str
    action: str
    pending_windows: int
    failed_pairs: int
    no_data: int
    completed: int
    auth_cause: str | None = None

    @property
    def needs_operator(self) -> bool:
        """True iff a human must act before more data can land (the loud case).

        ``quota_exhausted`` counts: the day's quota is spent, so the operator must
        wait for the IST-midnight reset (or re-pull then) — the auto-resume cannot
        finish today on its own.
        """
        return self.kind in ("auth_expired", "quota_exhausted", "transient_residual")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the pull-all route / structured log."""
        return {
            "kind": self.kind,
            "headline": self.headline,
            "action": self.action,
            "needs_operator": self.needs_operator,
            "pending_windows": self.pending_windows,
            "failed_pairs": self.failed_pairs,
            "no_data": self.no_data,
            "completed": self.completed,
            "auth_cause": self.auth_cause,
        }


def classify_pull(
    *,
    auth_aborted: bool,
    still_failed_windows: int,
    failed_pairs: int,
    no_data: int,
    completed: int,
    auth_cause: str | None = None,
    quota_exhausted: bool = False,
) -> PullVerdict:
    """Classify a finished pull into ONE prioritised, actionable :class:`PullVerdict`.

    Priority (worst first), so the operator always sees the most urgent action:

    1. **auth_expired** — the token died mid-pull (``auth_aborted``). Most urgent:
       no retry can recover until the token is regenerated. ``pending_windows``
       counts the still-failed windows + quarantined pairs left behind.
    2. **quota_exhausted** — the day's 100,000-call Dhan quota is spent
       (``quota_exhausted``) with windows still pending: the auto-resume paused
       itself rather than burn a dead quota; the operator re-pulls after the
       IST-midnight reset (DEC-AUTO-RESUME-QUOTA-001). Ranked just under a dead
       token because, like it, NO amount of immediate retrying can land a bar
       today — only the reset can.
    3. **transient_residual** — windows are still failing (``still_failed_windows``)
       or pairs were quarantined (``failed_pairs``) but the token is alive: a
       re-pull / the auto-retry layer will finish them.
    4. **known_gap_only** — nothing completed and nothing failed, but the vendor
       returned empty for some symbols (``no_data``): Dhan genuinely has no
       intraday for them (reference-only). Expected, not an error.
    5. **clean** — everything that could complete did.

    Pure + deterministic (no I/O / clock). ``auth_cause`` is preserved ONLY on
    the ``auth_expired`` verdict.
    """
    if auth_aborted:
        return _make(
            "auth_expired", still_failed_windows, failed_pairs, no_data, completed, auth_cause
        )
    if quota_exhausted:
        return _make(
            "quota_exhausted", still_failed_windows, failed_pairs, no_data, completed, None
        )
    if still_failed_windows > 0 or failed_pairs > 0:
        return _make(
            "transient_residual", still_failed_windows, failed_pairs, no_data, completed, None
        )
    if completed == 0 and no_data > 0:
        return _make("known_gap_only", still_failed_windows, failed_pairs, no_data, completed, None)
    return _make("clean", still_failed_windows, failed_pairs, no_data, completed, None)


def _make(
    kind: PullVerdictKind,
    still_failed_windows: int,
    failed_pairs: int,
    no_data: int,
    completed: int,
    auth_cause: str | None,
) -> PullVerdict:
    """Build a :class:`PullVerdict` for *kind* with the canonical headline + action."""
    return PullVerdict(
        kind=kind,
        headline=_HEADLINE[kind],
        action=_ACTION[kind],
        pending_windows=still_failed_windows + failed_pairs,
        failed_pairs=failed_pairs,
        no_data=no_data,
        completed=completed,
        auth_cause=auth_cause,
    )


__all__ = ["PullVerdict", "PullVerdictKind", "classify_pull"]
