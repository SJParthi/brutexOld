"""Unit tests for :mod:`brutex.dashboard.oneclick_badges` (DEC-INSTITUTIONAL-ONECLICK-001).

Pure O(1) dict-lookup module (no I/O, no FastAPI) — mirrors
``tests/unit/test_verdict_banner.py``'s style for the sibling
``verdict_banner.banner_for()`` helper.
"""

from __future__ import annotations

import pytest
from brutex.dashboard.oneclick_badges import StatusBadge, check_badge_for, link_badge_for


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
