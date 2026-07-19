"""Frozen-corpus parity goldens for the Rust cascade_wire laws (slice 2a).

DEC-RUST-CASCADE-RESULTS-001 phase B. The ``cascade_wire.json`` fixture under
``fixtures/cascade_rust_parity/`` was captured from the PRE-conversion
pure-Python ``brutex.engine.cascade_wire`` law bodies (commit f7ea834) — the
capture-first discipline — and is BYTE-FROZEN (the generator refuses
regeneration without ``BRUTEX_REGEN_CASCADE_PARITY=1``). This suite replays
the SAME corpus inputs (built by ``_cascade_wire_parity_corpus`` — shared
with the generator, so inputs cannot drift) through the post-conversion
Rust-backed public API and asserts EXACT equality:

* manifest bytes byte-exact + object round-trip equality;
* wire-report bytes byte-exact + the dict projection == the parsed bytes;
* every parse outcome — a success re-serialises to the frozen bytes, a
  failure reproduces the frozen exception TYPE name AND the FULL ``str``
  (including the raw ``IndexError``/``AttributeError`` escapes and the
  ``version: true`` bool==int pun that PARSES — the frozen law);
* the math trio's floats compared as ``repr`` strings (bit-exact, signed
  zeros distinguished; NaN/inf are not JSON-native).

A missing fixture FAILS loudly (never skips) — the corpus is the conversion
contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from brutex.engine.cascade_wire import (
    _finite,
    _lbt_to_dict,
    _max_drawdown_return_units,
    _per_bar_returns,
    _stage3_to_dict,
    manifest_from_json,
    manifest_to_json,
    serialize_wire_report,
    wire_report_to_dict,
)

from tests.golden._cascade_wire_parity_corpus import (
    finite_cases,
    manifest_cases,
    max_drawdown_cases,
    parse_payload_cases,
    per_bar_returns_cases,
    wire_report_cases,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "cascade_rust_parity" / "cascade_wire.json"
)


def _fixture() -> dict[str, Any]:
    assert FIXTURE_PATH.is_file(), (
        f"frozen parity fixture missing at {FIXTURE_PATH} — the corpus is the "
        "conversion contract (DEC-RUST-CASCADE-RESULTS-001); never regenerate "
        "without BRUTEX_REGEN_CASCADE_PARITY=1"
    )
    loaded = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _family(name: str) -> dict[str, dict[str, Any]]:
    return {case["name"]: case["expect"] for case in _fixture()[name]}


def _flat_family(name: str) -> dict[str, dict[str, Any]]:
    """The math-trio families carry ``expect_repr(s)`` on the case itself."""
    return {case["name"]: case for case in _fixture()[name]}


class TestManifestParity:
    """Every frozen manifest case serialises byte-exactly + round-trips."""

    @pytest.mark.parametrize("case_name", [name for name, _ in manifest_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        manifest = dict(manifest_cases())[case_name]
        expect = _family("manifests")[case_name]
        blob = manifest_to_json(manifest)
        assert blob.decode("utf-8") == expect["json"]
        # The capture recorded the pre-conversion round-trip verdict — pin it
        # AND reproduce it on the converted parser (object equality).
        assert expect["roundtrip_ok"] is True
        assert manifest_from_json(blob) == manifest


class TestWireReportParity:
    """Every frozen report case serialises byte-exactly, dict included."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in wire_report_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        report = dict(wire_report_cases())[case_name]
        expect = _family("reports")[case_name]
        blob = serialize_wire_report(report)
        assert blob.decode("utf-8") == expect["json"]
        payload = wire_report_to_dict(report)
        assert payload == expect["dict"]
        # Floats survive the json round-trip exactly (repr law) — the frozen
        # supplementary pin must hold on the converted projection too.
        assert expect["dict_matches_json"] is True
        assert payload == json.loads(blob)


class TestParseParity:
    """Every frozen parse outcome reproduces — bytes OR exception verbatim.

    The expectations include NON-CascadeWireError escapes (a raw
    ``IndexError`` for a 2-element proximity list, a raw ``AttributeError``
    for a non-dict spec entry) and the ``version: true`` bool==int PUN that
    parses OK — those ARE the frozen law.
    """

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in parse_payload_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        payload = dict(parse_payload_cases())[case_name]
        expect = _family("parses")[case_name]
        if "error_type" in expect:
            with pytest.raises(BaseException) as exc_info:
                manifest_from_json(payload)
            assert type(exc_info.value).__name__ == expect["error_type"]
            assert str(exc_info.value) == expect["error_str"]
        else:
            manifest = manifest_from_json(payload)
            assert manifest_to_json(manifest).decode("utf-8") == expect["json"]


class TestDictDelegatorParity:
    """The standalone ``_stage3_to_dict`` / ``_lbt_to_dict`` delegators agree
    with the frozen manifest render — ONE Rust projection path, the shim
    delegators are thin faces over it."""

    def test_delegators_match_manifest_payload(self) -> None:
        manifest = dict(manifest_cases())["full_stage3_lbt_roll"]
        payload = json.loads(manifest_to_json(manifest))
        assert manifest.stage3 is not None
        assert _stage3_to_dict(manifest.stage3) == payload["stage3"]
        assert manifest.leaderboard_trials is not None
        assert [_lbt_to_dict(record) for record in manifest.leaderboard_trials] == payload[
            "leaderboard_trials"
        ]


class TestMathTrioParity:
    """The math trio reproduces the frozen ``repr`` strings bit-exactly."""

    def test_finite(self) -> None:
        frozen = _flat_family("finite")
        for name, value in finite_cases():
            assert repr(_finite(value)) == frozen[name]["expect_repr"], name

    def test_per_bar_returns(self) -> None:
        frozen = _flat_family("per_bar_returns")
        for name, equity, start in per_bar_returns_cases():
            got = [repr(r) for r in _per_bar_returns(equity, start)]
            assert got == frozen[name]["expect_reprs"], name

    def test_max_drawdown(self) -> None:
        frozen = _flat_family("max_drawdown")
        for name, series in max_drawdown_cases():
            assert repr(_max_drawdown_return_units(series)) == frozen[name]["expect_repr"], name
