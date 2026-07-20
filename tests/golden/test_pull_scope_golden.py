"""Frozen pull-scope note-surgery goldens (DEC-RUST-PULL-STATE-001).

The committed fixtures are the PRE-conversion oracle capture (see
``generate_pull_scope_golden.py``); these tests recompute the SAME tables
from the LIVE code via the shared ``_pull_scope_harness`` and assert
equality — so the Rust conversion lane is proven a pure re-hosting (surgery
bytes identical) at every phase, on BOTH flag arms (the default flag-OFF run
here; the flag-ON twin lives in ``tests/unit/test_pull_scope_rust_seam.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.golden._pull_scope_harness import (
    ANCHOR_NAMES,
    compute_note_surgery_table,
    render_rust_fixture,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "pull_scope_note_surgery.json"
TSV_FIXTURE = FIXTURES / "pull_scope_note_surgery_rust.tsv"


def _frozen_table() -> dict[str, object]:
    payload = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


class TestPullScopeGolden:
    def test_live_table_matches_frozen_json(self) -> None:
        """The LIVE module reproduces the frozen oracle table EXACTLY."""
        assert compute_note_surgery_table() == _frozen_table()

    def test_tsv_twin_matches_json_rendering(self) -> None:
        """Cross-format pin: the committed TSV is the committed JSON's exact
        rendering — the two fixture files are ONE oracle and can never drift
        apart (the dependency-free cargo test consumes the TSV)."""
        table = _frozen_table()
        assert TSV_FIXTURE.read_text(encoding="utf-8") == render_rust_fixture(table)

    def test_anchor_names_all_frozen(self) -> None:
        """Every drift-pinned anchor name is present in the frozen capture
        (a harness edit dropping one must fail loudly, not shrink the pin)."""
        table = _frozen_table()
        anchors = table["anchors"]
        assert isinstance(anchors, dict)
        assert set(anchors) == set(ANCHOR_NAMES)
        assert all(isinstance(v, str) and v for v in anchors.values())

    def test_corpus_covers_the_warn_and_passthrough_arms(self) -> None:
        """Anti-vacuous teeth: the frozen corpus genuinely exercises the
        surgery-miss WARN arm (both axes, both half-drift directions), the
        no-WARN full-drift pass-through, and the refusal family."""
        table = _frozen_table()
        spot = table["spot_only_surgery"]
        fno = table["fno_only_surgery"]
        scope = table["scope"]
        assert isinstance(spot, list) and isinstance(fno, list) and isinstance(scope, list)
        for rows in (spot, fno):
            warned = [r for r in rows if r["miss_warned"]]
            assert {(r["prefix_present"], r["clause_present"]) for r in warned} == {
                (True, False),
                (False, True),
            }
            assert any(
                not r["miss_warned"] and not r["prefix_present"] and not r["clause_present"]
                for r in rows
            )
            assert any(r["prefix_present"] and r["clause_present"] for r in rows)
        assert any("error" in r for r in scope)
        assert any(r.get("ok") == "spot-only" for r in scope)
        assert any(r.get("ok") == "fno-only" for r in scope)
        assert any(r["token"] is None for r in scope)
