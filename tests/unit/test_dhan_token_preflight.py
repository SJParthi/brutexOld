"""Tests for `brutex.adapters.dhan.token_preflight` — the pure token pre-flight classifier.

100% line+branch on the wheel-free pure core (DEC-TOKEN-PREFLIGHT-001): the
three-state classifier (valid / expiring-soon / dead), the probe-result wrapper,
and the DEAD → ``auth_expired`` :class:`PullVerdict` mapping (which REUSES
``classify_pull`` from DEC-PULL-VERDICT-001 #485 — no second verdict vocabulary).
The live probe (:func:`probe_token_live`) is ``# pragma: no cover`` (real token +
network), so it is not exercised here; its decision logic IS the covered pure
:func:`classify_token`.
"""

from __future__ import annotations

import pytest
from brutex.adapters.dhan.pull_verdict import PullVerdict
from brutex.adapters.dhan.token_keepalive import RENEW_BEFORE_EXPIRY_S
from brutex.adapters.dhan.token_preflight import (
    TokenPreflight,
    classify_token,
    classify_token_lifetime,
    preflight_pull_verdict,
)


def test_threshold_matches_keepalive_renew_window() -> None:
    """The default expiring-soon threshold IS the keepalive's 12h renew window (CLAUDE.md §11).

    Pins the single-source-of-truth coupling: the pre-flight calls a token
    "expiring soon" exactly when the 12h keepalive would renew it, so the two
    layers can never drift apart.
    """
    pre = classify_token_lifetime(RENEW_BEFORE_EXPIRY_S - 1.0)
    assert pre.state == "expiring_soon"
    pre_ok = classify_token_lifetime(RENEW_BEFORE_EXPIRY_S)
    assert pre_ok.state == "valid"


def test_classify_lifetime_dead_when_expired() -> None:
    """Remaining life ≤ 0 ⇒ DEAD (a pull would only fire doomed 401s)."""
    pre = classify_token_lifetime(-120.0)
    assert pre.state == "dead"
    assert pre.is_dead is True
    assert pre.is_valid is False
    assert pre.needs_renew is True
    assert pre.remaining_seconds == -120.0


def test_classify_lifetime_dead_at_exactly_zero() -> None:
    """Remaining life of exactly 0 is DEAD (boundary: ``<= 0``)."""
    assert classify_token_lifetime(0.0).state == "dead"


def test_classify_lifetime_expiring_soon() -> None:
    """0 < remaining < threshold ⇒ expiring_soon (usable, but renew before a long pull)."""
    pre = classify_token_lifetime(3600.0)  # 1h left, < 12h default
    assert pre.state == "expiring_soon"
    assert pre.is_dead is False
    assert pre.is_valid is False
    assert pre.needs_renew is True


def test_classify_lifetime_valid() -> None:
    """Remaining ≥ threshold ⇒ valid (ample life, safe to start a long pull)."""
    pre = classify_token_lifetime(20 * 3600.0)
    assert pre.state == "valid"
    assert pre.is_valid is True
    assert pre.needs_renew is False


def test_classify_lifetime_custom_threshold() -> None:
    """A custom expiring threshold shifts the valid/expiring boundary."""
    # 1h remaining is "valid" under a 30-minute threshold.
    assert classify_token_lifetime(3600.0, expiring_threshold_s=1800.0).state == "valid"
    # ...and "expiring_soon" under a 2-hour threshold.
    assert classify_token_lifetime(3600.0, expiring_threshold_s=7200.0).state == "expiring_soon"


def test_classify_token_probe_auth_failed_is_dead() -> None:
    """A probe auth failure ⇒ DEAD regardless of any stale expiry guess."""
    pre = classify_token(probe_auth_failed=True, expiry_deadline=1_000_000.0, now=0.0)
    assert pre.state == "dead"
    assert pre.remaining_seconds is None  # probe death carries no usable lifetime


def test_classify_token_probe_ok_unknown_expiry_is_valid() -> None:
    """A successful probe with no parseable expiry ⇒ valid (it clearly works now)."""
    pre = classify_token(probe_auth_failed=False, expiry_deadline=None, now=123.0)
    assert pre.state == "valid"
    assert pre.remaining_seconds is None


def test_classify_token_from_deadline_and_now() -> None:
    """A successful probe with a learned deadline classifies the remaining life."""
    # deadline 13h after now ⇒ valid (≥ 12h); 1h after now ⇒ expiring_soon.
    assert (
        classify_token(probe_auth_failed=False, expiry_deadline=13 * 3600.0, now=0.0).state
        == "valid"
    )
    assert (
        classify_token(probe_auth_failed=False, expiry_deadline=3600.0, now=0.0).state
        == "expiring_soon"
    )
    # deadline already in the past ⇒ dead.
    assert classify_token(probe_auth_failed=False, expiry_deadline=100.0, now=200.0).state == "dead"


def test_preflight_pull_verdict_dead_maps_to_auth_expired() -> None:
    """A DEAD pre-flight yields the loud ``auth_expired`` verdict (via classify_pull)."""
    verdict = preflight_pull_verdict(classify_token_lifetime(-1.0))
    assert isinstance(verdict, PullVerdict)
    assert verdict.kind == "auth_expired"
    assert verdict.needs_operator is True
    # The reason summary is carried as the auth_cause by default.
    assert verdict.auth_cause is not None
    assert "DEAD" in verdict.auth_cause


def test_preflight_pull_verdict_explicit_cause_preserved() -> None:
    """An explicit *auth_cause* overrides the default summary on the verdict."""
    verdict = preflight_pull_verdict(
        classify_token_lifetime(-1.0), auth_cause="HTTP 401 token expired"
    )
    assert verdict is not None
    assert verdict.auth_cause == "HTTP 401 token expired"


@pytest.mark.parametrize("remaining", [3600.0, 20 * 3600.0])
def test_preflight_pull_verdict_alive_returns_none(remaining: float) -> None:
    """A non-dead pre-flight returns ``None`` so the caller simply proceeds."""
    assert preflight_pull_verdict(classify_token_lifetime(remaining)) is None


def test_to_dict_shape() -> None:
    """``to_dict`` is the stable JSON contract for a route / structured log."""
    pre = classify_token_lifetime(-5.0)
    assert pre.to_dict() == {
        "state": "dead",
        "remaining_seconds": -5.0,
        "reason": pre.reason,
        "is_dead": True,
        "needs_renew": True,
    }


def test_preflight_is_frozen() -> None:
    """``TokenPreflight`` is an immutable value object."""
    pre = TokenPreflight(state="valid", remaining_seconds=1.0, reason="ok")
    with pytest.raises(AttributeError):
        pre.state = "dead"  # type: ignore[misc]
