"""Unit tests for :mod:`brutex.adapters.dhan.capability_matrix` — 100% line+branch.

Also serves as the honest verified-vs-unverified capability record (a golden-style pin).
"""

from __future__ import annotations

from brutex.adapters.dhan import capability_matrix as cm


def test_matrix_covers_every_endpoint() -> None:
    names = {cap.name for cap in cm.DHAN_CAPABILITY_MATRIX}
    assert names == {
        "historical_daily",
        "intraday",
        "option_chain",
        "expiry_list",
        "rolling_option",
        "renew_token",
        "validate_token",
        "scrip_master",
    }


def test_every_endpoint_is_verified_shape() -> None:
    # every endpoint's path + REQUEST shape + limits are VERIFIED; the UNVERIFIED
    # aspects (response envelopes / field names / value codes) are named separately
    assert all(cap.verified for cap in cm.DHAN_CAPABILITY_MATRIX)


def test_verified_and_unverified_split() -> None:
    verified = {cap.name for cap in cm.verified_capabilities()}
    unverified = {cap.name for cap in cm.unverified_capabilities()}
    assert verified.isdisjoint(unverified)
    assert verified | unverified == {cap.name for cap in cm.DHAN_CAPABILITY_MATRIX}
    # ONLY validate_token is fully verified (round-2 honesty recount: expiry_list's
    # + option_chain's response ENVELOPES and renew_token's dhanClientId header
    # spelling are model-inferred, so those endpoints carry _UNVERIFIED aspects)
    assert verified == {"validate_token"}
    # historical/intraday/option_chain/expiry_list/rolling/renew/scrip carry aspects
    assert "intraday" in unverified
    assert "rolling_option" in unverified
    assert "expiry_list" in unverified
    assert "renew_token" in unverified


def test_probe_gates_enumerated() -> None:
    gates = cm.DHAN_UNVERIFIED_PROBE_GATES
    assert any("price unit" in g for g in gates)
    assert any("Greek field names" in g for g in gates)
    assert any("SEM_" in g for g in gates)
    assert any("DH-902" in g for g in gates)
    assert any("expired-futures" in g for g in gates)
    assert any("subscription" in g for g in gates)
    # round-2 additions: the two response ENVELOPES + the dhanClientId header
    # spelling + the value-code encoding widening of the scrip-master gate
    assert any("expiry-list response ENVELOPE" in g for g in gates)
    assert any("option-chain response ENVELOPE" in g for g in gates)
    assert any("dhanClientId" in g for g in gates)
    assert any("value-code encoding" in g for g in gates)
    # deduplicated + non-empty
    assert len(gates) == len(set(gates))
    assert gates


def test_endpoint_capability_default_unverified_empty() -> None:
    cap = cm.EndpointCapability(
        name="x", endpoint="/x", verb="GET", verified=True, coverage="c", limits="l"
    )
    assert cap.unverified == ()
