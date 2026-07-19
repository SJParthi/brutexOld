"""Pure presentation helper for the /universe pull-verdict banner (DEC-VERDICT-BANNER-001).

DEC-PULL-VERDICT-001 (PR #485) made a finished spot pull carry ONE prioritised,
actionable :class:`~brutex.adapters.ingest_common.PullVerdict` — emitted as
the ``"verdict"`` object in the ``/api/v1/universe/pull-all`` JSON
(:meth:`PullVerdict.to_dict`: ``kind``, ``headline``, ``action``,
``needs_operator``, ``pending_windows``, …). That verdict is in the JSON but the
``/universe`` page never SHOWED it. This module is the pure half of the banner:
it maps the *already-computed* verdict onto a small, deterministic
:class:`VerdictBanner` presentation — a CSS modifier class + a human colour name
(red = ``credentials_missing`` / ``auth_expired`` / ``mass_empty_halt``, amber =
``quota_exhausted`` / ``cancelled`` / ``transient_residual``, grey =
``quarantined_symbols`` / ``known_gap_only``, green = ``clean``) + a
``show_retry_button`` flag — that the coverage-excluded HTML/JS renders verbatim.

**COMMON (CLAUDE.md §11):** no verdict field is re-derived here — ``headline`` /
``action`` / ``pending_windows`` are passed through verbatim and the retry button
is shown exactly when the verdict's own ``needs_operator`` flag is set, so the one
classification authority stays :func:`classify_pull`. The button itself is wired
to the EXISTING manual ``POST /api/v1/universe/retry-failed`` engine
(DEC-INGEST-AUTO-RETRY-001 / PR #483) — this helper only decides whether to show
it.

**Honest envelope (Golden Rule #10):** :func:`banner_for` is a pure O(1) mapping
(one dict lookup + field copies, bounded memory) — it only *displays* the verdict;
it never fetches data, renews a token, or retries anything itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

#: Verdict kind → (CSS modifier class, human colour name). The keys are the
#: exact, mutually-exclusive
#: :data:`~brutex.adapters.ingest_common.PullVerdictKind` values; the colours
#: are the operator-facing severity decree (red/amber/grey/green). ``quota_exhausted``
#: shares the amber "paused — resume later" lane (the auto-resume paused itself on
#: the daily quota; not broken, just waiting for the IST-midnight reset —
#: DEC-AUTO-RESUME-QUOTA-001), distinct from the red ``auth_expired`` "broken token".
_KIND_STYLE: Final[dict[str, tuple[str, str]]] = {
    # ``credentials_missing`` is the PRE-pull refusal (a REAL pull refused
    # loudly because the Groww credentials did not resolve — nothing fetched,
    # nothing written; DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001). Red like
    # ``auth_expired``: only the operator can fix it.
    "credentials_missing": ("verdict--red", "red"),
    "auth_expired": ("verdict--red", "red"),
    # ``mass_empty_halt`` is the mass-empty circuit-breaker HALT (a suspected
    # vendor soft-throttle / mis-addressing episode stopped the pull before
    # cementing more windows; DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001). Red: the
    # operator must triage before re-pulling — a blind retry is what the
    # breaker exists to prevent. (Was MISSING here while ``classify_pull``
    # could emit it — a real halted pull 500'd the pull-verdict route; the
    # kind-set drift pin in tests/unit/test_verdict_banner.py now keeps this
    # map exactly equal to ``PullVerdictKind``.)
    "mass_empty_halt": ("verdict--red", "red"),
    "quota_exhausted": ("verdict--amber", "amber"),
    # ``cancelled`` is the operator's cooperative Stop (DEC-JOB-COOPERATIVE-
    # CANCEL-001 continuation-v3): an honest amber PARTIAL — "re-run the pull
    # to resume the remainder" — sharing the transient_residual lane (the
    # retry button targets the same re-pull engine), never green.
    "cancelled": ("verdict--amber", "amber"),
    "transient_residual": ("verdict--amber", "amber"),
    "quarantined_symbols": ("verdict--grey", "grey"),
    "known_gap_only": ("verdict--grey", "grey"),
    "clean": ("verdict--green", "green"),
}


@dataclass(frozen=True, slots=True)
class VerdictBanner:
    """The display-ready banner for one finished-pull verdict.

    Immutable + JSON-serialisable. Every field is either a pure presentation
    decision (``css_class`` / ``colour`` / ``show_retry_button``) or a verbatim
    pass-through of a :class:`~brutex.adapters.ingest_common.PullVerdict`
    field (``kind`` / ``headline`` / ``action`` / ``pending_windows``) — never a
    re-derivation (CLAUDE.md §11 COMMON).
    """

    kind: str
    css_class: str
    colour: str
    headline: str
    action: str
    pending_windows: int
    show_retry_button: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the ``/api/v1/universe/pull-verdict`` route."""
        return {
            "kind": self.kind,
            "css_class": self.css_class,
            "colour": self.colour,
            "headline": self.headline,
            "action": self.action,
            "pending_windows": self.pending_windows,
            "show_retry_button": self.show_retry_button,
        }


def banner_for(verdict: Mapping[str, Any]) -> VerdictBanner:
    """Map a finished-pull *verdict* dict onto its display-ready :class:`VerdictBanner`.

    *verdict* is a :meth:`~brutex.adapters.ingest_common.PullVerdict.to_dict`
    mapping. The ``kind`` selects the colour + CSS class; the retry button is
    shown exactly when the verdict's own ``needs_operator`` flag is set (the
    button targets the existing ``/api/v1/universe/retry-failed`` engine —
    DEC-INGEST-AUTO-RETRY-001). ``headline`` / ``action`` / ``pending_windows``
    are passed through verbatim. Pure + O(1) (one dict lookup + field copies).

    Parameters
    ----------
    verdict
        The verdict mapping from the pull-all JSON — must carry ``kind``,
        ``headline``, ``action``, ``pending_windows`` and ``needs_operator``.

    Returns
    -------
    VerdictBanner
        The display-ready banner.

    Raises
    ------
    ValueError
        If ``kind`` is not one of the known
        :data:`~brutex.adapters.ingest_common.PullVerdictKind` values — a
        fail-closed guard so an unknown kind is never rendered uncoloured.
    """
    kind = verdict["kind"]
    style = _KIND_STYLE.get(kind)
    if style is None:
        raise ValueError(f"unknown verdict kind {kind!r} — expected one of {sorted(_KIND_STYLE)}")
    css_class, colour = style
    return VerdictBanner(
        kind=kind,
        css_class=css_class,
        colour=colour,
        headline=verdict["headline"],
        action=verdict["action"],
        pending_windows=int(verdict["pending_windows"]),
        show_retry_button=bool(verdict["needs_operator"]),
    )


__all__ = ["VerdictBanner", "banner_for"]
