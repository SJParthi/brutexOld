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
    # every endpoint's SHAPE is VERIFIED; the UNVERIFIED aspects are named separately
    assert all(cap.verified for cap in cm.DHAN_CAPABILITY_MATRIX)


def test_verified_and_unverified_split() -> None:
    verified = {cap.name for cap in cm.verified_capabilities()}
    unverified = {cap.name for cap in cm.unverified_capabilities()}
    assert verified.isdisjoint(unverified)
    assert verified | unverified == {cap.name for cap in cm.DHAN_CAPABILITY_MATRIX}
    # expiry_list / renew_token / validate_token are fully verified
    assert "expiry_list" in verified
    # historical/intraday/option_chain/rolling/scrip carry _UNVERIFIED aspects
    assert "intraday" in unverified
    assert "rolling_option" in unverified


def test_probe_gates_enumerated() -> None:
    gates = cm.DHAN_UNVERIFIED_PROBE_GATES
    assert any("price unit" in g for g in gates)
    assert any("Greek field names" in g for g in gates)
    assert any("SEM_" in g for g in gates)
    assert any("DH-902" in g for g in gates)
    assert any("expired-futures" in g for g in gates)
    assert any("subscription" in g for g in gates)
    # deduplicated + non-empty
    assert len(gates) == len(set(gates))
    assert gates


def test_endpoint_capability_default_unverified_empty() -> None:
    cap = cm.EndpointCapability(
        name="x", endpoint="/x", verb="GET", verified=True, coverage="c", limits="l"
    )
    assert cap.unverified == ()
