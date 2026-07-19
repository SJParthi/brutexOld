"""Frozen-corpus parity goldens for the Rust twin_detect laws (slice 2d).

DEC-RUST-CASCADE-RESULTS-001 phase A→B. The ``twin_detect.json`` fixture under
``fixtures/cascade_rust_parity/`` was captured from the PRE-conversion
pure-Python ``brutex.engine.twin_detect`` law bodies — the capture-first
discipline — and is BYTE-FROZEN (the generator refuses regeneration without
``BRUTEX_REGEN_CASCADE_PARITY=1``). This suite replays the SAME corpus inputs
and scenario runners (``_twin_detect_parity_corpus`` — shared with the
generator, so the two sides can never drift) through the post-conversion
Rust-backed public API and asserts EXACT equality:

* the module constants (the ``bxtwin1|`` version prefix, the 0.90 threshold's
  exact double repr);
* every ``trade_fingerprint`` digest — the versioned ``|``/``\\n``
  serialization law frozen transitively through its BLAKE3 hex digests
  (empty list = the bare prefix; order significant; identity-only fields
  participate — the two extras-differ twins share one frozen digest; exact
  epoch-µs integers incl. pre-1970 negatives; the naive-datetime raw
  ``TypeError`` escape);
* every ``entry_ts_us_set`` signature (duplicates collapse; exact µs ints);
* every :class:`TwinCandidate` verdict — the bool==int mask puns (``True``
  accepted, ``bit_count() == 1``), the exact refusal messages (empty run_id,
  negative mask, the 64-lowercase-hex fingerprint grammar) and the raw
  ``TypeError`` escapes for non-string fingerprints;
* every ``_jaccard`` value as an EXACT f64 repr (the |∩|/|∪| division
  parity — the empty∩empty ``1.0`` definition included);
* every ``group_twins`` partition — tier-1 exact-fingerprint merges (entry
  sets ignored), the INCLUSIVE 0.90 tier-2 boundary (a 9-of-10 subset merges;
  17/19 stays separate), union-find transitivity (the drift chain whose A↔C
  pair alone is below threshold), the fewest-bits-then-run_id-ASC
  representative law, the ``family_id``-ASC members iteration order,
  input-order independence (the runner replays shuffled orderings and asserts
  equality), the threshold puns/refusals (``True`` acts as ``1.0``; ``0.0`` /
  ``-0.0`` / ``nan`` / ``inf`` refuse with the frozen reprs) and the
  duplicate-run_id refusal.

A missing fixture FAILS loudly (never skips) — the corpus is the conversion
contract. The hashseed-independence proof runs the WHOLE capture in two
subprocesses under different ``PYTHONHASHSEED`` values and pins byte-equal
canonical output (set/dict iteration order can never leak into the corpus).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from brutex.engine.twin_detect import FINGERPRINT_VERSION_PREFIX, TWIN_JACCARD_THRESHOLD

from tests.golden._twin_detect_parity_corpus import (
    candidate_cases,
    entry_set_cases,
    fingerprint_cases,
    group_cases,
    jaccard_cases,
    run_candidate_case,
    run_entry_set_case,
    run_fingerprint_case,
    run_group_case,
    run_jaccard_case,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "cascade_rust_parity" / "twin_detect.json"
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _fixture() -> dict[str, Any]:
    assert FIXTURE_PATH.is_file(), (
        f"missing frozen fixture {FIXTURE_PATH} — run tests/golden/"
        "gen_cascade_rust_parity.py against the PRE-conversion tree "
        "(DEC-RUST-CASCADE-RESULTS-001 capture-first discipline)"
    )
    loaded = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _family(name: str) -> dict[str, Any]:
    return {case["name"]: case["expect"] for case in _fixture()[name]}


class TestConstantsParity:
    """The version prefix + the pinned threshold's exact double."""

    def test_constants(self) -> None:
        assert _fixture()["constants"] == {
            "fingerprint_version_prefix": FINGERPRINT_VERSION_PREFIX,
            "twin_jaccard_threshold_repr": repr(TWIN_JACCARD_THRESHOLD),
        }


class TestFingerprintParity:
    """Every frozen BLAKE3 digest (and the naive-ts escape) reproduces."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in fingerprint_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        trades = dict(fingerprint_cases())[case_name]
        assert run_fingerprint_case(trades) == _family("fingerprint")[case_name]

    def test_identity_twins_share_one_digest(self) -> None:
        """The two extras-differ cases froze the SAME digest (the identity law)."""
        family = _family("fingerprint")
        assert family["identity_only_a"] == family["identity_only_b_extras_differ"]
        assert family["single_buy"] != family["single_sell_same_shape"]
        assert family["multi_chronological"] != family["multi_reversed_order_differs"]


class TestEntrySetParity:
    """Every frozen entry-timestamp signature reproduces (exact µs ints)."""

    @pytest.mark.parametrize("case_name", [name for name, _ in entry_set_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        trades = dict(entry_set_cases())[case_name]
        assert run_entry_set_case(trades) == _family("entry_set")[case_name]


class TestCandidateParity:
    """Every constructor verdict — puns, refusal messages, raw escapes."""

    @pytest.mark.parametrize("case_name", [name for name, _ in candidate_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        thunk = dict(candidate_cases())[case_name]
        assert run_candidate_case(thunk) == _family("candidate")[case_name]


class TestJaccardParity:
    """Every similarity value reproduces as its exact f64 repr."""

    @pytest.mark.parametrize("case_name", [name for name, _ in jaccard_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        pair = dict(jaccard_cases())[case_name]
        assert run_jaccard_case(pair) == _family("jaccard")[case_name]


class TestGroupTwinsParity:
    """Every frozen partition / refusal reproduces — orderings re-verified."""

    @pytest.mark.parametrize("case_name", [name for name, _ in group_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        case = dict(group_cases())[case_name]
        assert run_group_case(case) == _family("group")[case_name]


class TestHashseedIndependence:
    """The WHOLE capture is byte-identical across PYTHONHASHSEED values.

    Two subprocesses re-run ``capture_twin_detect`` under different hash seeds
    and print the canonical-JSON sha256 of the payload — set/dict iteration
    order can never leak into the frozen corpus.
    """

    def test_two_seeds_agree(self) -> None:
        snippet = (
            "import hashlib, json;"
            "from tests.golden.gen_cascade_rust_parity import capture_twin_detect;"
            "payload = capture_twin_detect();"
            "print(hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())"
        )
        digests: list[str] = []
        for seed in ("1", "20260719"):
            env = dict(os.environ)
            env["PYTHONHASHSEED"] = seed
            proc = subprocess.run(
                [sys.executable, "-c", snippet],
                cwd=_REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
            )
            digests.append(proc.stdout.strip().splitlines()[-1])
        assert digests[0] == digests[1]
        assert len(digests[0]) == 64
