"""Pure presentation-only status badges for the one-click sweep UI (DEC-INSTITUTIONAL-ONECLICK-001).

Mirrors :func:`~brutex.dashboard.verdict_banner.banner_for`'s pattern: maps an
*already-computed* status/state string onto a small, deterministic
presentation dict — a CSS modifier class + a human colour name + a display
label — that a caller renders verbatim. Pure O(1) dict lookups, zero I/O,
same discipline as ``verdict_banner.py`` (CLAUDE.md §11 COMMON: no verdict is
re-derived here, only styled).

Two vocabularies are covered:

- :func:`check_badge_for` — the R44 VERIFY-ME per-check status
  (``pass``/``fail``/``awaiting``, see
  :data:`~brutex.dashboard.oneclick_artifacts.VERIFY_ME_CHECKS` /
  :func:`~brutex.dashboard.oneclick_artifacts.read_verify_me`).
- :func:`link_badge_for` — the R41 chain-map link state
  (``pending``/``running``/``green``/``amber``/``red``, see
  :func:`~brutex.dashboard.oneclick_artifacts.read_chain_map`).

**No server-side templating exists in this dashboard** (module docstring on
:mod:`brutex.dashboard.oneclick_artifacts`: plain static HTML + client-side
JS, never Jinja) — so this module is not literally `import`-ed by a template.
It is instead the documented CANONICAL mapping that every client-side mirror
must stay byte-consistent with: ``templates/v29.html``'s pre-existing inline
``CM_STATE_CLASS`` map (chain-map link states, shipped in an earlier stage)
and ``templates/verify_me.html``'s inline ``STATUS_STYLE`` map (VERIFY-ME
check statuses, this stage) both carry an explicit comment pointing back
here, and ``tests/unit/test_oneclick_badges.py`` pins the exact
class/colour/label vocabulary both templates must match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class StatusBadge:
    """The display-ready badge for one status/state value.

    Immutable + JSON-serialisable. Every field is either a pure presentation
    decision (``css_class``/``colour``/``label``) or a verbatim echo of the
    input ``status`` — never a re-derivation.
    """

    status: str
    css_class: str
    colour: str
    label: str

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-friendly dict (e.g. for a future JSON-serving route)."""
        return {
            "status": self.status,
            "css_class": self.css_class,
            "colour": self.colour,
            "label": self.label,
        }


#: R44 VERIFY-ME per-check status → badge. Reuses the same green/red/amber
#: severity vocabulary as :mod:`brutex.dashboard.verdict_banner` (green =
#: trustworthy, red = broken, amber = not yet knowable — never a blank or
#: silently-omitted state).
_CHECK_STYLE: Final[dict[str, tuple[str, str, str]]] = {
    "pass": ("bx-chip ok", "green", "PASS"),
    "fail": ("bx-chip bad", "red", "FAIL"),
    "awaiting": ("bx-chip warn", "amber", "AWAITING"),
}

#: R41 chain-map link state → badge. Byte-consistent with
#: ``templates/v29.html``'s pre-existing inline ``CM_STATE_CLASS`` map
#: (``{pending: 'muted', running: 'info', green: 'ok', amber: 'warn', red:
#: 'bad'}``) — this module documents that mapping's canonical source.
_LINK_STYLE: Final[dict[str, tuple[str, str, str]]] = {
    "pending": ("bx-chip muted", "grey", "pending"),
    "running": ("bx-chip info", "blue", "running"),
    "green": ("bx-chip ok", "green", "green"),
    "amber": ("bx-chip warn", "amber", "amber"),
    "red": ("bx-chip bad", "red", "red"),
}


def check_badge_for(status: str) -> StatusBadge:
    """Map an R44 VERIFY-ME check ``status`` onto its display-ready badge.

    Parameters
    ----------
    status
        One of ``"pass"``/``"fail"``/``"awaiting"`` (the
        :data:`~brutex.dashboard.oneclick_artifacts._VERIFY_ME_STATUSES`
        vocabulary).

    Raises
    ------
    ValueError
        If *status* is not one of the three known values — a fail-closed
        guard so an unknown status is never rendered uncoloured / silently
        treated as a pass.
    """
    style = _CHECK_STYLE.get(status)
    if style is None:
        raise ValueError(
            f"unknown VERIFY-ME check status {status!r} — expected one of {sorted(_CHECK_STYLE)}"
        )
    css_class, colour, label = style
    return StatusBadge(status=status, css_class=css_class, colour=colour, label=label)


def link_badge_for(state: str) -> StatusBadge:
    """Map an R41 chain-map link ``state`` onto its display-ready badge.

    Parameters
    ----------
    state
        One of ``"pending"``/``"running"``/``"green"``/``"amber"``/``"red"``
        (the chain-map link state vocabulary from
        :func:`~brutex.dashboard.oneclick_artifacts.read_chain_map`).

    Raises
    ------
    ValueError
        If *state* is not one of the five known values.
    """
    style = _LINK_STYLE.get(state)
    if style is None:
        raise ValueError(
            f"unknown chain-map link state {state!r} — expected one of {sorted(_LINK_STYLE)}"
        )
    css_class, colour, label = style
    return StatusBadge(status=state, css_class=css_class, colour=colour, label=label)


__all__ = ["StatusBadge", "check_badge_for", "link_badge_for"]
