"""Frozen-corpus parity goldens for the Rust results_report laws (slice 2c).

DEC-RUST-CASCADE-RESULTS-001 phase A→B. The ``results_report.json`` fixture
under ``fixtures/cascade_rust_parity/`` was captured from the PRE-conversion
pure-Python ``brutex.engine.results_report`` law bodies — the capture-first
discipline — and is BYTE-FROZEN (the generator refuses regeneration without
``BRUTEX_REGEN_CASCADE_PARITY=1``). This suite replays the SAME corpus inputs
and scenario runners (``_results_report_parity_corpus`` — shared with the
generator, so the two sides can never drift) through the post-conversion
Rust-backed public API and asserts EXACT equality:

* the module constants (the SORT_ALIASES vocabulary, the default sort key,
  the quarantine dirname/audit relpath/log cap, the quarantine event name);
* every ``_resolve_sort_field`` outcome — the 7 aliases map, the 7 CANONICAL
  column names are REFUSED (a frozen quirk: the vocabulary is aliases-only),
  and the exact ``unknown sort key`` message with its sorted alias list;
* the ``_format_deflated`` ``+.4f`` law (``None → "—"``, the ``-0.0000``
  sign, display underflow, huge magnitudes) and the ``_format_conditions``
  empty→note / verbatim-passthrough law;
* every ``render_leaderboard`` byte law — the ``format_inr`` Western-grouped
  Decimal money render (exact beyond 2**53), the win/sharpe/dsr fixed-width
  displays, the origins ``src=grid|stage3|?`` markers (an EMPTY origins dict
  is NOT None — every row reads ``src=?``), the mask-0 no-condition note,
  the full-60-bit vocabulary decode line, unicode + lone-surrogate
  instrument passthrough;
* every ``_provisional_note`` arm — including the ``is True`` IDENTITY law
  (int ``1`` never counts, for ``provisional`` AND ``passed``);
* every ``_render_sidecar_group`` line or ValueError — the ``?`` fallbacks,
  the three pool-PBO honesty states, the ``float(pbo)`` coercion quirks
  (int / bool ``True`` / numeric string coerce; a junk string raises the
  frozen ``could not convert`` message), the dsr three-state stamp;
* every ``render_cascade_sidecar`` section — path-NORMALIZED (``<ROOT>``),
  including the ``version: true`` bool==int PUN that LOADS, the ``'1'`` /
  ``None`` repr refusals, and the group-level ValueError funneling into the
  LOUD ``UNREADABLE`` line;
* the end-to-end ``results_show_report`` text assembly over a REAL
  ResultsStore tree — header counts, ``min(top, n)`` law, sharpe/trades
  tie→run_id-ASC order, the two provenance notes, src markers from a REAL
  cascade manifest, the sidecar append law, and the frozen refusal messages
  (``top must be >= 1``, unknown sort, missing root — normalized).

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
from brutex.engine.results_report import (
    _AUDIT_RELPATH,
    _QUARANTINE_DIRNAME,
    _QUARANTINE_LOG_PATH_CAP,
    DEFAULT_SORT_BY,
    SNAPSHOT_QUARANTINE_EVENT,
    SORT_ALIASES,
)

from tests.golden._results_report_parity_corpus import (
    format_conditions_cases,
    format_deflated_cases,
    leaderboard_cases,
    provisional_note_cases,
    run_format_conditions_case,
    run_format_deflated_case,
    run_leaderboard_case,
    run_provisional_note_case,
    run_show_report_case,
    run_sidecar_group_case,
    run_sidecar_render_case,
    run_sort_field_case,
    show_report_cases,
    sidecar_group_cases,
    sidecar_render_cases,
    sort_field_cases,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "cascade_rust_parity" / "results_report.json"
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
    """The sort vocabulary + the read-back's pinned path/log constants."""

    def test_constants(self) -> None:
        assert _fixture()["constants"] == {
            "sort_aliases": dict(sorted(SORT_ALIASES.items())),
            "default_sort_by": DEFAULT_SORT_BY,
            "quarantine_dirname": _QUARANTINE_DIRNAME,
            "audit_relpath": list(_AUDIT_RELPATH),
            "quarantine_log_path_cap": _QUARANTINE_LOG_PATH_CAP,
            "snapshot_quarantine_event": SNAPSHOT_QUARANTINE_EVENT,
        }


class TestSortFieldParity:
    """Aliases map; canonical columns + junk are refused with the frozen message."""

    @pytest.mark.parametrize("case_name", [name for name, _ in sort_field_cases()], ids=lambda n: n)
    def test_case(self, case_name: str) -> None:
        token = dict(sort_field_cases())[case_name]
        assert run_sort_field_case(token) == _family("sort_field")[case_name]


class TestFormatDeflatedParity:
    """The ``+.4f`` display law over hostile floats reproduces byte-exactly."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in format_deflated_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        value = dict(format_deflated_cases())[case_name]
        assert run_format_deflated_case(value) == _family("format_deflated")[case_name]


class TestFormatConditionsParity:
    """The empty→note law and the verbatim non-empty passthrough reproduce."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in format_conditions_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        value = dict(format_conditions_cases())[case_name]
        assert run_format_conditions_case(value) == _family("format_conditions")[case_name]


class TestLeaderboardParity:
    """Every frozen winners-table render reproduces byte-exactly."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in leaderboard_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        case = dict(leaderboard_cases())[case_name]
        assert run_leaderboard_case(case) == _family("leaderboard")[case_name]


class TestProvisionalNoteParity:
    """Every counted-label arm — the ``is True`` identity law included."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in provisional_note_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        report = dict(provisional_note_cases())[case_name]
        assert run_provisional_note_case(report) == _family("provisional_note")[case_name]


class TestSidecarGroupParity:
    """Every group line / ValueError — the float-coercion quirks included."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in sidecar_group_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        entry = dict(sidecar_group_cases())[case_name]
        assert run_sidecar_group_case(entry) == _family("sidecar_group")[case_name]


class TestSidecarRenderParity:
    """Every whole-section render — normalized, the version bool pun included."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in sidecar_render_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        content = dict(sidecar_render_cases())[case_name]
        assert run_sidecar_render_case(content) == _family("sidecar_render")[case_name]


class TestShowReportParity:
    """The end-to-end text assembly over a REAL tree reproduces — normalized."""

    @pytest.mark.parametrize(
        "case_name", [name for name, _ in show_report_cases()], ids=lambda n: n
    )
    def test_case(self, case_name: str) -> None:
        case = dict(show_report_cases())[case_name]
        assert run_show_report_case(case) == _family("show_report")[case_name]


class TestHashseedIndependence:
    """The WHOLE capture is byte-identical across PYTHONHASHSEED values.

    Two subprocesses re-run ``capture_results_report`` under different hash
    seeds and print the canonical-JSON sha256 of the payload — set/dict
    iteration order can never leak into the frozen corpus.
    """

    def test_two_seeds_agree(self) -> None:
        snippet = (
            "import hashlib, json;"
            "from tests.golden.gen_cascade_rust_parity import capture_results_report;"
            "payload = capture_results_report();"
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
