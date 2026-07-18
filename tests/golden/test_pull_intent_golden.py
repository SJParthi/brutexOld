"""The frozen pull-intent verdict goldens (DEC-RUST-PULL-STATE-001, Wave 2E).

Recomputes the FULL fixture tables from the LIVE
``brutex.dashboard.pull_intent`` via the shared harness and asserts
structural equality against the committed pre-conversion capture — the
byte-level oracle every Rust-seam phase must preserve:

* every sidecar-bytes corruption/valid shape's store-level verdict (the
  loaded intent or the corrupt/absent answer) + the corrupt-DELETE effect +
  the mechanical kernel-input classification (taxonomy parity by
  CONSTRUCTION: the JSON decode stays Python, so CPython's failure taxonomy
  — ``UnicodeDecodeError`` ⊂ ``ValueError``, nesting-bomb
  ``RecursionError``, ``OverflowError`` — folds into the ONE
  not-a-Mapping classification; design risk #3 has no serde/hand-rolled
  parser to drift);
* the ``run_auto_resume`` arm lattice (probe-BEFORE-cap order, the
  bump-before-launch counter, the probe-never-called-without-intent law);
* the refused-start restore lattice (DEC-PULL-SCOPED-ALL-ONECLICK-001 r3
  F-1) — leave-absent / clear-orphan / restore-prior-verbatim;
* the resume-env token law over hostile unicode (the CPython
  ``str.isspace``-only ``\\x1c`` whitespace class included).

The TSV twin (``pull_intent_kernel_cases.tsv``) is recomputed + compared
too — the SAME rows the crate's ``cargo test`` replays (the cross-language
pin can therefore never drift from the Python oracle silently).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.golden._pull_intent_harness import compute_all_tables, compute_kernel_tsv

if TYPE_CHECKING:
    from collections.abc import Iterator

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
_JSON_PATH = _FIXTURE_DIR / "pull_intent_verdicts.json"
_TSV_PATH = _FIXTURE_DIR / "pull_intent_kernel_cases.tsv"


@pytest.fixture(autouse=True)
def _hermetic_resume_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The ambient escape hatch must never skew the recomputed tables."""
    monkeypatch.delenv("BRUTEX_PULL_INTENT_RESUME", raising=False)
    monkeypatch.delenv("BRUTEX_PULL_STATE_RUST", raising=False)
    yield


class TestPullIntentGolden:
    def test_fixtures_exist(self) -> None:
        assert _JSON_PATH.is_file(), "run tests/golden/generate_pull_intent_golden.py once"
        assert _TSV_PATH.is_file(), "run tests/golden/generate_pull_intent_golden.py once"

    def test_module_tables_match_frozen_fixture(self, tmp_path: Path) -> None:
        frozen = json.loads(_JSON_PATH.read_text(encoding="utf-8"))
        live = compute_all_tables(tmp_path)
        # Per-table compare first (a readable diff), then the whole capture.
        for table in ("parse", "resume", "restore", "env"):
            assert live[table] == frozen[table], f"pull_intent {table} table drifted"
        assert live == frozen

    def test_kernel_tsv_matches_frozen_fixture(self, tmp_path: Path) -> None:
        frozen = _TSV_PATH.read_text(encoding="utf-8")
        live = compute_all_tables(tmp_path)
        recomputed = compute_kernel_tsv(live["parse"], live["resume"], live["restore"], live["env"])
        assert recomputed == frozen

    def test_corpus_covers_every_verdict_class(self) -> None:
        """Anti-vacuity: both parse verdicts, every resume arm, every restore
        arm and both env answers are genuinely present in the frozen corpus."""
        frozen = json.loads(_JSON_PATH.read_text(encoding="utf-8"))
        parse_verdicts = {row["intent"] is None for row in frozen["parse"]}
        assert parse_verdicts == {True, False}
        deleted = {row["file_deleted"] for row in frozen["parse"]}
        assert deleted == {True, False}
        assert {row["arm"] for row in frozen["resume"]} == {0, 1, 2, 3}
        assert {row["arm"] for row in frozen["restore"]} == {0, 1, 2, 3}
        assert {row["enabled"] for row in frozen["env"]} == {True, False}
        # the beyond-u64 Python-arm row is present AND excluded from the TSV
        assert any(not row["kernel_row"] for row in frozen["resume"])
        tsv = _TSV_PATH.read_text(encoding="utf-8")
        assert not any(
            line.split("\t")[1] == row["id"]
            for row in frozen["resume"]
            if not row["kernel_row"]
            for line in tsv.splitlines()
            if line.startswith("resume\t")
        )
