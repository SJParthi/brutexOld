"""Pure presentation helper for the /universe pull-verdict banner (DEC-VERDICT-BANNER-001).

DEC-PULL-VERDICT-001 (PR #485) made a finished spot pull carry ONE prioritised,
actionable :class:`~brutex.adapters.dhan.pull_verdict.PullVerdict` — emitted as
the ``"verdict"`` object in the ``/api/v1/universe/pull-all`` JSON
(:meth:`PullVerdict.to_dict`: ``kind``, ``headline``, ``action``,
``needs_operator``, ``pending_windows``, …). That verdict is in the JSON but the
``/universe`` page never SHOWED it. This module is the pure half of the banner:
it maps the *already-computed* verdict onto a small, deterministic
:class:`VerdictBanner` presentation — a CSS modifier class + a human colour name
(red = ``auth_expired``, amber = ``transient_residual``, grey = ``known_gap_only``,
green = ``clean``) + a ``show_retry_button`` flag — that the coverage-excluded
HTML/JS renders verbatim.

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

#: Verdict kind → (CSS modifier class, human colour name). The four kinds are the
#: exact, mutually-exclusive
#: :data:`~brutex.adapters.dhan.pull_verdict.PullVerdictKind` values; the colours
#: are the operator-facing severity decree (red/amber/grey/green).
_KIND_STYLE: Final[dict[str, tuple[str, str]]] = {
    "auth_expired": ("verdict--red", "red"),
    "transient_residual": ("verdict--amber", "amber"),
    "known_gap_only": ("verdict--grey", "grey"),
    "clean": ("verdict--green", "green"),
}


@dataclass(frozen=True, slots=True)
class VerdictBanner:
    """The display-ready banner for one finished-pull verdict.

    Immutable + JSON-serialisable. Every field is either a pure presentation
    decision (``css_class`` / ``colour`` / ``show_retry_button``) or a verbatim
    pass-through of a :class:`~brutex.adapters.dhan.pull_verdict.PullVerdict`
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

    *verdict* is a :meth:`~brutex.adapters.dhan.pull_verdict.PullVerdict.to_dict`
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
        If ``kind`` is not one of the four known
        :data:`~brutex.adapters.dhan.pull_verdict.PullVerdictKind` values — a
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
