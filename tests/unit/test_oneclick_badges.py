"""Unit tests for the status-badge helpers in
:mod:`brutex.dashboard.oneclick_artifacts` (DEC-INSTITUTIONAL-ONECLICK-001).

These were originally a separate ``oneclick_badges`` module; they now live
in ``oneclick_artifacts.py`` (the single P4 adapter module the ownership
grant allows — round-3 review finding) alongside the rest of the one-click
artifact readers. Pure O(1) dict-lookup functions (no I/O, no FastAPI) —
mirrors ``tests/unit/test_verdict_banner.py``'s style for the sibling
``verdict_banner.banner_for()`` helper.
"""

from __future__ import annotations

import pytest
from brutex.dashboard.oneclick_artifacts import StatusBadge, check_badge_for, link_badge_for


@pytest.mark.parametrize(
    ("status", "css_class", "colour", "label"),
    [
        ("pass", "bx-chip ok", "green", "PASS"),
        ("fail", "bx-chip bad", "red", "FAIL"),
        ("awaiting", "bx-chip warn", "amber", "AWAITING"),
    ],
)
def test_check_badge_for_known_statuses(status: str, css_class: str, colour: str, label: str) -> None:
    badge = check_badge_for(status)
    assert isinstance(badge, StatusBadge)
    assert badge.status == status
    assert badge.css_class == css_class
    assert badge.colour == colour
    assert badge.label == label


def test_check_badge_for_unknown_status_raises() -> None:
    with pytest.raises(ValueError, match="unknown VERIFY-ME check status"):
        check_badge_for("maybe")


@pytest.mark.parametrize(
    ("state", "css_class", "colour", "label"),
    [
        ("pending", "bx-chip muted", "grey", "pending"),
        ("running", "bx-chip info", "blue", "running"),
        ("green", "bx-chip ok", "green", "green"),
        ("amber", "bx-chip warn", "amber", "amber"),
        ("red", "bx-chip bad", "red", "red"),
    ],
)
def test_link_badge_for_known_states(state: str, css_class: str, colour: str, label: str) -> None:
    badge = link_badge_for(state)
    assert isinstance(badge, StatusBadge)
    assert badge.status == state
    assert badge.css_class == css_class
    assert badge.colour == colour
    assert badge.label == label


def test_link_badge_for_unknown_state_raises() -> None:
    with pytest.raises(ValueError, match="unknown chain-map link state"):
        link_badge_for("purple")


def test_status_badge_to_dict_round_trips_fields() -> None:
    badge = check_badge_for("pass")
    d = badge.to_dict()
    assert d == {"status": "pass", "css_class": "bx-chip ok", "colour": "green", "label": "PASS"}


def test_status_badge_is_frozen() -> None:
    badge = check_badge_for("pass")
    with pytest.raises(AttributeError):
        badge.status = "fail"  # type: ignore[misc]


def test_check_and_link_vocabularies_cover_every_backend_value() -> None:
    """Pin against the adapter's own known vocabularies so a future new
    status/state value is caught here rather than crashing a template."""
    from brutex.dashboard.oneclick_artifacts import _VERIFY_ME_STATUSES

    for status in _VERIFY_ME_STATUSES:
        check_badge_for(status)  # must not raise

    for state in ("pending", "running", "green", "amber", "red"):
        link_badge_for(state)  # must not raise


def test_status_style_maps_match_templates() -> None:
    """Pin ``verify_me.html``'s inline ``STATUS_STYLE`` and ``v29.html``'s
    inline ``CM_STATE_CLASS`` against the Python vocabulary/css classes they
    claim to mirror (module docstring above), so a future edit to either
    template's hardcoded JS object silently drifting from
    :func:`check_badge_for` / :func:`link_badge_for` fails a test instead of
    only a code-review comment.
    """
    import re

    from brutex.dashboard.app import _load_verify_me_template, _render_index_html

    verify_me_html = _load_verify_me_template()
    status_style_match = re.search(r"var STATUS_STYLE = \{(.*?)\};", verify_me_html, re.DOTALL)
    assert status_style_match is not None, "STATUS_STYLE object not found in verify_me.html"
    status_style_src = status_style_match.group(1)
    for status in ("pass", "fail", "awaiting"):
        badge = check_badge_for(status)
        # css_class is "bx-chip <cls>" in Python; the template's local object
        # stores just "<cls>" and prefixes "bx-chip " at render time.
        expected_cls = badge.css_class.removeprefix("bx-chip ")
        assert re.search(rf"{status}:\s*\{{\s*cls:\s*\"{expected_cls}\"", status_style_src), (
            f"verify_me.html STATUS_STYLE[{status!r}] cls should be {expected_cls!r}"
        )
        assert re.search(rf'{status}:\s*\{{[^}}]*label:\s*"{badge.label}"', status_style_src), (
            f"verify_me.html STATUS_STYLE[{status!r}] label should be {badge.label!r}"
        )

    v29_html = _render_index_html()
    cm_state_match = re.search(r"var CM_STATE_CLASS = \{(.*?)\};", v29_html)
    assert cm_state_match is not None, "CM_STATE_CLASS object not found in v29.html"
    cm_state_src = cm_state_match.group(1)
    for state in ("pending", "running", "green", "amber", "red"):
        badge = link_badge_for(state)
        expected_cls = badge.css_class.removeprefix("bx-chip ")
        assert re.search(rf"{state}:\s*'{expected_cls}'", cm_state_src), (
            f"v29.html CM_STATE_CLASS[{state!r}] should be {expected_cls!r}"
        )
