"""Frozen-corpus parity goldens for the Rust baseline laws (slice 2d).

DEC-RUST-CASCADE-RESULTS-001 phase A→B. The ``baseline.json`` fixture under
``fixtures/cascade_rust_parity/`` was captured from the PRE-conversion
pure-Python ``brutex.engine.baseline`` law bodies — the capture-first
discipline — and is BYTE-FROZEN (the generator refuses regeneration without
``BRUTEX_REGEN_CASCADE_PARITY=1``). This suite replays the SAME corpus inputs
and scenario runners (``_baseline_parity_corpus`` — shared with the generator,
so the two sides can never drift) through the post-conversion Rust-backed
public API and asserts EXACT equality:

* the module constants (the schema version, the exact B8 payload key set, the
  2-field session-entry law);
* every ``compute_baseline`` series — the first-session-first-bar-OPEN anchor
  (kept VERBATIM, even off the 2dp grid), the last-bar-per-IST-session close
  law (the ``ist_trade_date`` midnight edge included), the
  Decimal-division-at-context-precision-then-float ``total_return_pct``
  (exact f64 reprs), the verbatim ``source_note``, load-order independence
  (the runner replays permutations) and the exact refusal messages in their
  frozen ORDER (empty → duplicate-timestamp → non-positive anchor);
* every :class:`BaselineSeries` constructor verdict (the bool
  ``total_return_pct`` pun that passes ``isfinite``, the exact
  start/end-mismatch message, nan/inf refusals);
* every ``baseline_filename`` output — NO sanitization; hostile instruments
  (``/``, ``&``, unicode, ``..``) pass through VERBATIM, empty refuses;
* every ``_price_str`` ROUND_HALF_UP 2dp snap (exact Decimal, no float);
* every ``write_baseline`` artifact TEXT — sorted keys · compact separators ·
  ASCII escapes (unicode AND lone-surrogate instruments) · trailing newline ·
  the write-time price snap · the JSON ``true`` a bool total serializes to —
  plus the ``baseline.written`` log shape and the empty-hash refusal;
* every ``load_baseline`` damage verdict — each ``None`` arm's frozen
  ``baseline.load_invalid`` reason, the ``version: true`` / ``1.0`` bool==int
  and float==int puns that LOAD, the bool ``total_return_pct`` EXCLUSION, the
  Decimal-``NaN``-comparison ``InvalidOperation`` funnel, and the quirky
  payloads that legitimately LOAD (``Infinity`` anchor, ``NaN`` session
  price, duplicate-JSON-keys-last-wins, lone-surrogate instrument).

A missing fixture FAILS loudly (never skips) — the corpus is the conversion
contract. The hashseed-independence proof runs the WHOLE capture in two
subprocesses under different ``PYTHONHASHSEED`` values and pins byte-equal
canonical output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from brutex.engine.baseline import _PAYLOAD_KEYS, _SESSION_FIELDS, BASELINE_SCHEMA_VERSION

from tests.golden._baseline_parity_corpus import (
    compute_cases,
    filename_cases,
    load_cases,
    price_str_cases,
    run_compute_case,
    run_filename_case,
    run_load_case,
    run_price_str_case,
    run_series_ctor_case,
    run_write_case,
    series_ctor_cases,
    write_cases,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "cascade_rust_parity" / "baseline.json"
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
    """The schema version + the pinned B8 payload key set."""

    def test_constants(self) -> None:
        assert _fixture()["constants"] == {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "payload_keys": sorted(_PAYLOAD_KEYS),
            "session_fields": _SESSION_FIELDS,
        }


class TestComputeBaselineParity:
    """Every frozen series / refusal reproduces — orderings re-verified."""

    @pytest.mark.parametrize("case_name", [name for name, _ in compute_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        case = dict(compute_cases())[case_name]
        assert run_compute_case(case) == _family("compute")[case_name]


class TestSeriesCtorParity:
    """Every constructor verdict — the bool-total pun included."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in series_ctor_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        thunk = dict(series_ctor_cases())[case_name]
        assert run_series_ctor_case(thunk) == _family("series_ctor")[case_name]


class TestFilenameParity:
    """No sanitization — hostile instruments pass through verbatim."""

    @pytest.mark.parametrize("case_name", [name for name, _ in filename_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        instrument = dict(filename_cases())[case_name]
        assert run_filename_case(instrument) == _family("filename")[case_name]


class TestPriceStrParity:
    """The ROUND_HALF_UP 2dp serialization snap reproduces exactly."""

    @pytest.mark.parametrize("case_name", [name for name, _ in price_str_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        literal = dict(price_str_cases())[case_name]
        assert run_price_str_case(literal) == _family("price_str")[case_name]


class TestWriteBaselineParity:
    """Every artifact TEXT byte law + the written-log shape reproduces."""

    @pytest.mark.parametrize("case_name", [name for name, _ in write_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        thunk = dict(write_cases())[case_name]
        assert run_write_case(thunk) == _family("write")[case_name]


class TestLoadBaselineParity:
    """Every damage verdict / pun-load reproduces with its frozen reason."""

    @pytest.mark.parametrize("case_name", [name for name, _ in load_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        raw = dict(load_cases())[case_name]
        assert run_load_case(raw) == _family("load")[case_name]


class TestHashseedIndependence:
    """The WHOLE capture is byte-identical across PYTHONHASHSEED values.

    Two subprocesses re-run ``capture_baseline`` under different hash seeds
    and print the canonical-JSON sha256 of the payload — set/dict iteration
    order can never leak into the frozen corpus.
    """

    def test_two_seeds_agree(self) -> None:
        snippet = (
            "import hashlib, json;"
            "from tests.golden.gen_cascade_rust_parity import capture_baseline;"
            "payload = capture_baseline();"
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
