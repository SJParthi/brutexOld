"""Golden byte-equality for the token pre-flight + until-clean verdict surfaces.

DEC-TOKEN-PREFLIGHT-001. The operator-facing summaries + the JSON ``to_dict``
contracts are DATA (not f-strings), so they must be byte-stable across refactors
— a downstream alerter / dashboard renders them verbatim. This pins the exact
``reason`` text per token state, the ``to_dict`` shapes, and the DEAD →
``auth_expired`` :class:`PullVerdict` headline + action (reused from
DEC-PULL-VERDICT-001 #485 — proving the pre-flight emits the SAME loud verdict a
finished pull does).
"""

from __future__ import annotations

from brutex.adapters.dhan.token_preflight import (
    classify_token_lifetime,
    preflight_pull_verdict,
)
from brutex.adapters.dhan.until_clean import UntilCleanOutcome, UntilCleanRound


def test_token_state_summaries_are_byte_stable() -> None:
    """Each token state renders its exact, pinned operator summary."""
    assert (
        classify_token_lifetime(-1.0).reason
        == "🔑 Dhan token is DEAD — regenerate at web.dhan.co before pulling."
    )
    assert (
        classify_token_lifetime(3600.0).reason
        == "⏳ Dhan token is expiring soon — renew before a long bulk pull."
    )
    assert (
        classify_token_lifetime(20 * 3600.0).reason
        == "✅ Dhan token healthy — ample life remaining."
    )


def test_preflight_to_dict_golden() -> None:
    """The pre-flight JSON contract is byte-stable."""
    assert classify_token_lifetime(13 * 3600.0).to_dict() == {
        "state": "valid",
        "remaining_seconds": 13 * 3600.0,
        "reason": "✅ Dhan token healthy — ample life remaining.",
        "is_dead": False,
        "needs_renew": False,
    }


def test_dead_preflight_emits_auth_expired_verdict_golden() -> None:
    """A DEAD pre-flight maps to the EXACT shared ``auth_expired`` verdict (#485 vocab)."""
    verdict = preflight_pull_verdict(classify_token_lifetime(-1.0))
    assert verdict is not None
    assert verdict.kind == "auth_expired"
    assert verdict.headline == "🔑 Dhan token expired mid-pull — the pull aborted."
    assert verdict.action == (
        "Regenerate the 24-hour token at web.dhan.co, then re-pull. "
        "A retry can NEVER succeed on a dead token — only a fresh token can."
    )
    assert verdict.needs_operator is True


def test_until_clean_outcome_to_dict_golden() -> None:
    """The until-clean outcome JSON contract is byte-stable across all terminal kinds."""
    clean = UntilCleanOutcome(
        verdict="stop_clean",
        rounds_run=2,
        rounds=(UntilCleanRound(3, False), UntilCleanRound(0, False)),
    )
    assert clean.to_dict() == {
        "verdict": "stop_clean",
        "rounds_run": 2,
        "clean": True,
        "auth_aborted": False,
        "stopped_on_budget": False,
        "final_still_failed": 0,
    }
    dead = UntilCleanOutcome(
        verdict="stop_dead",
        rounds_run=1,
        rounds=(UntilCleanRound(7, True, auth_cause="DH-906 Invalid Token"),),
    )
    assert dead.pull_verdict().kind == "auth_expired"
    assert dead.pull_verdict().auth_cause == "DH-906 Invalid Token"
