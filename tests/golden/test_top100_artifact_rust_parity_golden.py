"""Frozen-corpus parity goldens for the Rust top100_artifact laws (slice 2b).

DEC-RUST-CASCADE-RESULTS-001 phase B. The ``top100_artifact.json`` fixture
under ``fixtures/cascade_rust_parity/`` was captured from the PRE-conversion
pure-Python ``brutex.engine.top100_artifact`` law bodies — the capture-first
discipline — and is BYTE-FROZEN (the generator refuses regeneration without
``BRUTEX_REGEN_CASCADE_PARITY=1``). This suite replays the SAME corpus inputs
and scenario runners (``_top100_artifact_parity_corpus`` — shared with the
generator, so the two sides can never drift) through the post-conversion
Rust-backed public API and asserts EXACT equality:

* the module constants + the deliberately-NOT-extended
  ``ENTRY_REQUIRED_KEYS`` set, pinned verbatim;
* every ``passes_hard_gates`` boundary verdict (both sides of all 5 gates,
  the ``None → 0.0`` win/loss substitution included);
* ``resolve_preset`` outcomes + the unknown-token degrade WARN;
* every ``rank_candidates`` preset order INCLUDING the tie ladders (dd tie →
  composite desc → losses asc → run_id ASC; the ``-0.0`` == ``0.0`` pun; the
  None-composite ranks-after-scored law; the alternates' run_id-ASC ties);
* every build outcome — full ``top100.json`` bytes byte-exact (unicode
  escapes, the lone-surrogate ``\\ud800`` escape, huge ints, the EXACT
  ``cpcv_pass_rate × 100.0`` float product, the cascade-join state table,
  the family collapse, the honesty header) or, for the 105-candidate cap
  case, sha256 + the full 100-entry rank order; refusals reproduce the
  frozen exception TYPE name AND the FULL ``str`` — including the RAW
  ``json.JSONDecodeError`` escape for an invalid agg JSON column and the
  build-succeeds-then-``rendered_bytes``-raises ``ValueError`` law for a
  NaN/inf aggregate float (``allow_nan=False`` — the frozen quirk);
* the constructor refusal strings (``Top100Candidate`` / ``PoolCounts``);
* the write→load behavioral pins (file bytes == rendered bytes, the exact
  stamp key set, zero temp residue, cascade-join-preserving round-trip);
* every ``load_top100`` damage class → its ONE named ``top100.load_invalid``
  reason — plus the two bool==int PUNS that legally LOAD
  (``schema_version: true`` and entry ``rank: true`` at position 1) and the
  additive cascade_join tolerance arms (non-object dropped; pre-change
  absent key) — those ARE the frozen law.

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
from brutex.engine.top100_artifact import (
    DEFAULT_TOP100_PRESET,
    ENTRY_REQUIRED_KEYS,
    GATE_MAX_CONSECUTIVE_LOSSES,
    GATE_MAX_DRAWDOWN_PCT,
    GATE_MIN_PROFIT_FACTOR,
    GATE_MIN_TRADES,
    GATE_MIN_WIN_LOSS_RATIO,
    GATE_MIN_WIN_RATE,
    TOP100_FILENAME,
    TOP100_LIMIT,
    TOP100_PRESETS,
    TOP100_SCHEMA_VERSION,
    TOP100_STAGE,
    TOP100_STAMP_FILENAME,
    passes_hard_gates,
    rank_candidates,
    resolve_preset,
)
from structlog.testing import capture_logs

from tests.golden._top100_artifact_parity_corpus import (
    build_cases,
    constructor_cases,
    gate_cases,
    load_scenarios,
    preset_resolution_cases,
    rank_cases,
    run_build_case,
    run_load_scenario,
    run_write_roundtrip,
    write_roundtrip_cases,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "cascade_rust_parity" / "top100_artifact.json"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _fixture() -> dict[str, Any]:
    assert FIXTURE_PATH.is_file(), (
        f"frozen parity fixture missing at {FIXTURE_PATH} — the corpus is the "
        "conversion contract (DEC-RUST-CASCADE-RESULTS-001); never regenerate "
        "without BRUTEX_REGEN_CASCADE_PARITY=1"
    )
    loaded = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _family(name: str) -> dict[str, Any]:
    return {case["name"]: case["expect"] for case in _fixture()[name]}


class TestConstantsParity:
    """The module vocabulary + the deliberately-unextended required-key set."""

    def test_constants(self) -> None:
        frozen = _fixture()["constants"]
        assert frozen == {
            "schema_version": TOP100_SCHEMA_VERSION,
            "stage": TOP100_STAGE,
            "filename": TOP100_FILENAME,
            "stamp_filename": TOP100_STAMP_FILENAME,
            "limit": TOP100_LIMIT,
            "default_preset": DEFAULT_TOP100_PRESET,
            "presets": list(TOP100_PRESETS),
            "entry_required_keys": sorted(ENTRY_REQUIRED_KEYS),
            "gate_min_trades": GATE_MIN_TRADES,
            "gate_min_profit_factor": GATE_MIN_PROFIT_FACTOR,
            "gate_max_drawdown_pct": GATE_MAX_DRAWDOWN_PCT,
            "gate_min_win_rate": GATE_MIN_WIN_RATE,
            "gate_min_win_loss_ratio": GATE_MIN_WIN_LOSS_RATIO,
            "gate_max_consecutive_losses": GATE_MAX_CONSECUTIVE_LOSSES,
        }


class TestHardGatesParity:
    """Every frozen gate boundary verdict reproduces."""

    @pytest.mark.parametrize("case_name", [name for name, _ in gate_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        candidate = dict(gate_cases())[case_name]
        assert passes_hard_gates(candidate.row, candidate.agg) == _family("gates")[case_name]


class TestPresetResolutionParity:
    """Known tokens pass through; unknown degrades to the default, logged."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in preset_resolution_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        token = dict(preset_resolution_cases())[case_name]
        expect = _family("preset_resolution")[case_name]
        with capture_logs() as logs:
            resolved = resolve_preset(token)
        warns = [e for e in logs if e.get("event") == "top100.unknown_preset"]
        assert resolved == expect["resolved"]
        assert bool(warns) == expect["warned"]
        assert (warns[0]["requested"] if warns else None) == expect["requested"]


class TestRankParity:
    """Every frozen preset order — tie ladders included — reproduces."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _preset, _pool in rank_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        _name, preset, pool = next(c for c in rank_cases() if c[0] == case_name)
        ordered = rank_candidates(pool, preset=preset)
        assert [c.row.run_id for c in ordered] == _family("ranks")[case_name]


class TestBuildParity:
    """Every frozen build outcome reproduces — bytes OR exception verbatim.

    The expectations include the RAW ``JSONDecodeError`` escape and the
    built-then-render ``ValueError`` (``allow_nan=False``) — the frozen law.
    """

    @pytest.mark.parametrize("case_name", [name for name, _ in build_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        case = dict(build_cases())[case_name]
        assert run_build_case(case) == _family("builds")[case_name]


class TestConstructorParity:
    """The Top100Candidate / PoolCounts refusal strings reproduce verbatim."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in constructor_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        thunk = dict(constructor_cases())[case_name]
        expect = _family("constructors")[case_name]
        with pytest.raises(BaseException) as exc_info:
            thunk()
        assert type(exc_info.value).__name__ == expect["error_type"]
        assert str(exc_info.value) == expect["error_str"]


class TestWriteRoundtripParity:
    """The write→load behavioral pins reproduce (stamp shape, atomic residue,
    byte-identical round-trip; ``produced_at_utc`` pinned only as a string)."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in write_roundtrip_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        build = dict(write_roundtrip_cases())[case_name]
        got = run_write_roundtrip(build)
        assert got == _family("write_roundtrip")[case_name]
        # Every behavioral pin in the frozen dict must ALSO be genuinely True
        # (an all-False capture would be a vacuous freeze).
        for key in (
            "file_equals_rendered",
            "stamp_run_id_matches",
            "content_hash_matches",
            "produced_at_is_str",
            "reload_equals_original",
            "reload_cascade_join_preserved",
        ):
            assert got[key] is True, key
        assert not [name for name in got["dir_files"] if name.startswith(".brutex-write-")]


class TestLoadParity:
    """Every frozen load outcome reproduces — the ONE named reason, or the
    loaded artifact's normalized re-rendered bytes (the bool==int puns LOAD)."""

    @pytest.mark.parametrize("case_name", [name for name, _ in load_scenarios()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        setup = dict(load_scenarios())[case_name]
        assert run_load_scenario(setup) == _family("load")[case_name]


class TestHashseedIndependence:
    """The WHOLE capture is byte-identical across PYTHONHASHSEED values.

    Two subprocesses re-run ``capture_top100_artifact`` under different hash
    seeds and print the canonical-JSON sha256 of the payload — set/dict
    iteration order can never leak into the frozen corpus.
    """

    def test_two_seeds_agree(self) -> None:
        snippet = (
            "import hashlib, json;"
            "from tests.golden.gen_cascade_rust_parity import capture_top100_artifact;"
            "payload = capture_top100_artifact();"
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
