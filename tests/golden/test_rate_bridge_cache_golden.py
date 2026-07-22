"""Frozen rate-bridge-cache decision goldens (``DEC-RUST-PULL-STATE-001``, Wave 3H).

The committed fixtures (``rate_bridge_cache_decisions.json`` + the serde-free
``rate_bridge_cache_kernel_cases.tsv`` projection) were captured by
``generate_rate_bridge_cache_golden.py`` on the PRE-conversion tree — they
are the oracle the Rust twin of the two clockless laws
(``memo_serve_arm`` + ``token_revalidates``) must satisfy bit-for-bit
(Golden Rule #1).  This test recomputes the SAME tables from the LIVE module
via the shared ``_rate_bridge_cache_harness`` and asserts structural
equality, plus pins:

* the TSV is EXACTLY the projection of the JSON (the cargo fixture test
  consumes the TSV, so the two must never drift);
* the load-bearing boundary rows are genuinely in the lattice (anti-vacuous:
  ``age == ttl`` refuses the fast path — the strict-``<`` law; ``ttl == 0``
  disables it; the TTL-wins-over-inflight ordering; the clock-skew clamp;
  the never-trust-a-``None``-token reread).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.golden._rate_bridge_cache_harness import (
    SERVE_ARM_NAMES,
    compute_payload,
    compute_tsv,
    f64_bits,
    f64_from_bits,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "rate_bridge_cache_decisions.json"
TSV_FIXTURE = FIXTURES / "rate_bridge_cache_kernel_cases.tsv"


@pytest.fixture(scope="module")
def frozen() -> dict[str, Any]:
    payload = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


@pytest.fixture(scope="module")
def live() -> dict[str, Any]:
    payload = json.loads(json.dumps(compute_payload(), allow_nan=False))
    assert isinstance(payload, dict)
    return payload


def _serve(payload: dict[str, Any], case_id: str) -> dict[str, Any]:
    for row in payload["serve"]:
        if row["id"] == case_id:
            return row
    raise AssertionError(f"serve case {case_id!r} not in fixture")


def _token(payload: dict[str, Any], case_id: str) -> dict[str, Any]:
    for row in payload["token"]:
        if row["id"] == case_id:
            return row
    raise AssertionError(f"token case {case_id!r} not in fixture")


class TestFrozenGolden:
    def test_serve_rows_match_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["serve"] == frozen["serve"]

    def test_token_rows_match_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["token"] == frozen["token"]

    def test_tsv_is_the_json_projection(self, frozen: dict[str, Any]) -> None:
        assert TSV_FIXTURE.read_text(encoding="utf-8") == compute_tsv(frozen)

    def test_bits_round_trip(self) -> None:
        import math

        for value in (0.0, 2.0, -1.0, math.inf):
            assert f64_from_bits(f64_bits(value)) == value
        assert math.isnan(f64_from_bits(f64_bits(math.nan)))


class TestAntiVacuous:
    """The load-bearing boundary rows are genuinely in the frozen lattice."""

    def test_exact_boundary_refuses_the_fast_path(self, frozen: dict[str, Any]) -> None:
        row = _serve(frozen, "boundary_exact_owner")
        assert row["age"] == row["ttl"]  # genuinely ON the boundary
        assert SERVE_ARM_NAMES[row["arm"]] == "owner_refresh"
        assert _serve(frozen, "just_under_boundary")["arm"] == 0  # strict <

    def test_ttl_zero_disables_the_fast_path(self, frozen: dict[str, Any]) -> None:
        assert _serve(frozen, "ttl_zero_owner")["arm"] == 2
        assert _serve(frozen, "ttl_zero_follower")["arm"] == 1

    def test_ttl_wins_over_inflight(self, frozen: dict[str, Any]) -> None:
        assert _serve(frozen, "fresh_hit_inflight_ttl_wins")["arm"] == 0

    def test_clock_skew_clamps_to_zero_age(self, frozen: dict[str, Any]) -> None:
        row = _serve(frozen, "skew_future_probe_clamps_hit")
        assert row["age"] == {"bits": f64_bits(0.0)}
        assert row["arm"] == 0

    def test_nan_ttl_never_caches(self, frozen: dict[str, Any]) -> None:
        assert _serve(frozen, "ttl_nan_owner")["arm"] == 2

    def test_none_token_is_never_trusted(self, frozen: dict[str, Any]) -> None:
        assert _token(frozen, "probe_error_rereads")["revalidates"] is False
        assert _token(frozen, "both_none_rereads")["revalidates"] is False

    def test_equal_empty_tokens_revalidate(self, frozen: dict[str, Any]) -> None:
        assert _token(frozen, "equal_empty_revalidates")["revalidates"] is True
