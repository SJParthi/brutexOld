"""Frozen pull-stall decision goldens (DEC-RUST-PULL-STATE-001, Wave 1C).

The committed fixtures (``pull_stall_verdicts.json`` + the serde-free
``pull_stall_verdicts.tsv`` projection) were captured by
``generate_pull_stall_golden.py`` on the PRE-conversion tree — they are the
oracle the Rust port of ``brutex/dashboard/pull_stall.py``'s decision plane
must satisfy bit-for-bit (Golden Rule #1). This test recomputes the SAME
tables from the LIVE module via the shared ``_pull_stall_harness`` and
asserts structural equality, plus pins:

* the TSV is EXACTLY the projection of the JSON (one truth, two encodings —
  the cargo fixture test consumes the TSV, so the two must never drift);
* the FAIL-SAFE resolver posture (junk → silent audited default — the
  deliberate opposite of the lane's fail-loud env family) including its
  pinned quirk arms (``"inf"`` accepted, ``"nan"`` passed through);
* the bit-pattern encodings round-trip (``f64_bits`` ∘ ``f64_from_bits``).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from tests.golden._pull_stall_harness import (
    WEDGE_ARM_NAMES,
    compute_payload,
    compute_tsv,
    f64_bits,
    f64_from_bits,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "pull_stall_verdicts.json"
TSV_FIXTURE = FIXTURES / "pull_stall_verdicts.tsv"


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


class TestFrozenGolden:
    def test_wedge_table_matches_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["wedge"] == frozen["wedge"]

    def test_alive_table_matches_frozen(self, frozen: dict[str, Any], live: dict[str, Any]) -> None:
        assert live["alive"] == frozen["alive"]

    def test_resolver_tables_match_frozen(
        self, frozen: dict[str, Any], live: dict[str, Any]
    ) -> None:
        assert live["resolvers"] == frozen["resolvers"]

    def test_tsv_is_the_projection_of_the_json(self, frozen: dict[str, Any]) -> None:
        """One truth, two encodings — the cargo-side TSV can never drift."""
        assert TSV_FIXTURE.read_text(encoding="utf-8") == compute_tsv(frozen)

    def test_lattice_is_nonvacuous(self, frozen: dict[str, Any]) -> None:
        """Every kernel token value appears in the frozen wedge lattice."""
        seen = {row["expected_token"] for row in frozen["wedge"]}
        assert seen == set(range(len(WEDGE_ARM_NAMES)))
        assert {row["expected_due"] for row in frozen["alive"]} == {True, False}

    def test_arm_names_align_with_tokens(self, frozen: dict[str, Any]) -> None:
        for row in frozen["wedge"]:
            assert row["expected_arm"] == WEDGE_ARM_NAMES[row["expected_token"]]


class TestFailSafeResolverPosture:
    """The resolvers are FAIL-SAFE (junk → silent default) — pinned, never 'fixed'."""

    @pytest.mark.parametrize("kind", ["stall_timeout", "wedge_multiplier"])
    def test_junk_rows_resolve_to_the_default(self, frozen: dict[str, Any], kind: str) -> None:
        rows = frozen["resolvers"][kind]
        junk = [r for r in rows if r["raw"] in (None, "", "   ", "junk", "12abc", "-1", "0")]
        assert junk, "the frozen sweep must carry the junk arms"
        assert all(r["is_default"] for r in junk)

    @pytest.mark.parametrize("kind", ["stall_timeout", "wedge_multiplier"])
    def test_pinned_quirk_arms(self, frozen: dict[str, Any], kind: str) -> None:
        by_raw = {r["raw"]: r for r in frozen["resolvers"][kind]}
        # "inf" > 0 is accepted as-is; "nan" slips the `<= 0.0` guard — both
        # are the audited module's real behaviour, frozen so a future port
        # cannot silently 'fix' them (behaviour-preserving law).
        assert by_raw["inf"]["resolved"] == "inf"
        assert by_raw["nan"]["resolved"] == "nan"
        assert math.isnan(f64_from_bits(by_raw["nan"]["resolved_bits"]))
        assert by_raw["-inf"]["is_default"]


class TestBitEncoding:
    @pytest.mark.parametrize(
        "value",
        [0.0, -0.0, 1.5, 3600.0, float("inf"), float("-inf"), 2.0**-1074],
    )
    def test_roundtrip(self, value: float) -> None:
        assert f64_from_bits(f64_bits(value)) == value
        # Sign-exactness (0.0 vs -0.0 compare equal — check the bits).
        assert f64_bits(f64_from_bits(f64_bits(value))) == f64_bits(value)

    def test_nan_roundtrip_is_bitwise(self) -> None:
        bits = f64_bits(float("nan"))
        assert math.isnan(f64_from_bits(bits))
        assert f64_bits(f64_from_bits(bits)) == bits
