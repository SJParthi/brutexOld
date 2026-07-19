"""Golden byte-equality oracle for the top-100 artifact (DEC-SWEEP-RESULTS-DEPTH-001, WP5).

A fixed, fully hand-specified TWO-TUPLE candidate fixture — five pooled
winners across two tuple refs, one cross-bucket twin pair, one gate-failer,
one unscored composite — is built into the artifact and the rendered
``top100.json`` bytes are locked by sha256. Unlike the Parquet goldens this
digest is PURE JSON (no polars/zstd build sensitivity): any change to the
entry projection, the ranking law, the family collapse, the honesty header or
the JSON rendering law moves the digest and MUST be a deliberate, ledgered
re-lock (the ``_GOLDEN_CONTENT_HASH`` discipline).

Also locks the R42 rebuild law: a corrupted artifact fails the stamp gate
(``load_top100 → None``) and the deterministic rebuild converges to the SAME
locked bytes.

Regenerate (deterministic) after a DELIBERATE schema change::

    .venv/bin/python -c "import tests.golden.test_top100_artifact_golden as g; g.print_digest()"
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from brutex.engine.top100_artifact import (
    PoolCounts,
    Top100Artifact,
    build_top100,
    load_top100,
    write_top100,
)
from brutex.engine.twin_detect import TwinCandidate, group_twins

from tests.fakes.wp4_twin_fixtures import fingerprint_seed
from tests.fakes.wp5_depth_fixtures import make_candidate

_RUN = "a1" * 32

#: Locked digest (sha256 hex of the rendered top100.json bytes).
#: Regeneration is a deliberate, ledgered act — see the module docstring.
#: RE-LOCKED at the 2026-07-15 club-O integration merge of
#: DEC-RESILIENCE-STREAKS-001 (#1395) with main: the merged writer emits the
#: additive per-entry ``resilience`` block ON TOP of the two deltas the
#: 2026-07-12 re-lock below records (root ``cascade_join`` meta + per-entry
#: ``cascade`` object + ``metrics.exits."session_gap"``); the digest below is
#: re-derived from the merged writer's rendered bytes (additive-only — every
#: committed pre-change artifact fixture still loads byte-untouched).
#: Prior lock history:
#: RE-LOCKED at the 2026-07-12 merge of DEC-RESULTS-CASCADE-VERDICT-JOIN-001
#: (MIGRATION.md #1749) with main's DEC-TRADESIM-OPEN-SESSION-GUARD-001
#: (MIGRATION.md #1743): the merged writer emits BOTH additive deltas — the
#: #1749 root ``cascade_join`` meta + per-entry ``cascade`` object
#: (state/status ``no_report``/``not_validated`` when no report was threaded —
#: this fixture's arm; additive-only strip pin in
#: test_engine_top100_artifact.py against the frozen pre-change fixture) AND
#: the #1743 ``"session_gap": 0`` key in every entry's ``metrics.exits`` dict
#: (zero-count on this fixture — the wp5 fixture aggs carry no session-gap
#: closes). Readers stay presence-tolerant, so every committed pre-change
#: artifact fixture still loads byte-untouched.
_GOLDEN_TOP100_SHA256 = "9fa6a166f3370f6d3099bd86022e1d8282a2e9bef18a4efc1f735655d71ebed2"


def _fixture_artifact() -> Top100Artifact:
    # Two tuples: A (NIFTY 1minute) and B (NIFTY 5minute). The twins share
    # fingerprint_seed(7) ACROSS the two tuples — the cross-bucket collapse.
    twin_simple = make_candidate(
        1,
        fingerprint=fingerprint_seed(7),
        row_overrides={"mask_bits": 0b1, "max_drawdown_pct": 8.0},
        tuple_ref="NIFTY/1minute/bull_k1_aaaaaaaaaaaaaaaa",
    )
    twin_complex = make_candidate(
        2,
        fingerprint=fingerprint_seed(7),
        row_overrides={"mask_bits": 0b1011, "tf": "5minute", "max_drawdown_pct": 8.0},
        tuple_ref="NIFTY/5minute/bull_k3_bbbbbbbbbbbbbbbb",
        sl_paisa=None,
        tp_paisa=None,
    )
    calm_singleton = make_candidate(
        3,
        fingerprint=fingerprint_seed(3),
        row_overrides={"max_drawdown_pct": 4.0, "mask_bits": 0b110000},
        agg_overrides={"composite_score": None},
        tuple_ref="NIFTY/1minute/bull_k2_aaaaaaaaaaaaaaaa",
    )
    deep_singleton = make_candidate(
        4,
        fingerprint=fingerprint_seed(4),
        row_overrides={"max_drawdown_pct": 22.5, "mask_bits": 0b101},
        tuple_ref="NIFTY/5minute/bull_k2_bbbbbbbbbbbbbbbb",
    )
    gate_failer = make_candidate(
        5,
        fingerprint=fingerprint_seed(5),
        row_overrides={"profit_factor": 1.05},
        tuple_ref="NIFTY/1minute/bull_k1_aaaaaaaaaaaaaaaa",
    )
    candidates = [twin_simple, twin_complex, calm_singleton, deep_singleton, gate_failer]
    # Entry-instant signatures: the twin pair shares ONE set (tier-1 identical
    # fingerprints anyway); the singletons get DISJOINT sets so tier-2 can
    # never accidentally merge them.
    entry_sets = {
        twin_simple.row.run_id: frozenset({100, 200}),
        twin_complex.row.run_id: frozenset({100, 200}),
        calm_singleton.row.run_id: frozenset({300, 400}),
        deep_singleton.row.run_id: frozenset({500, 600}),
    }
    families = group_twins(
        [
            TwinCandidate(
                run_id=c.row.run_id,
                mask_bits=c.row.mask_bits,
                fingerprint=c.agg.fingerprint,
                entry_ts_us=entry_sets[c.row.run_id],
            )
            for c in candidates
            if c is not gate_failer
        ]
    )
    return build_top100(
        candidates,
        run_id=_RUN,
        preset="min_dd_first",
        families=families,
        counts=PoolCounts(stores=2, excluded_no_aggs=1, zero_trade_excluded=2, parity_excluded=1),
        data_snapshot_hashes=("snapB", "snapA"),
        baselines={"NIFTY": "baseline-NIFTY.json"},
    )


def print_digest() -> None:  # pragma: no cover - the regeneration helper
    print(hashlib.sha256(_fixture_artifact().rendered_bytes()).hexdigest())


def test_golden_artifact_bytes_are_locked() -> None:
    rendered = _fixture_artifact().rendered_bytes()
    assert hashlib.sha256(rendered).hexdigest() == _GOLDEN_TOP100_SHA256


def test_golden_build_is_deterministic() -> None:
    assert _fixture_artifact().rendered_bytes() == _fixture_artifact().rendered_bytes()


def test_golden_semantic_shape() -> None:
    """The version-robust logical contract behind the byte pin."""
    artifact = _fixture_artifact()
    # min_dd_first: calm (4.0) → the twin family's SIMPLEST member (8.0,
    # 1 bit beats 3 bits) → deep (22.5). Gate-failer excluded; twins collapsed.
    assert [entry["run_id"] for entry in artifact.entries] == [
        f"{3:064x}",
        f"{1:064x}",
        f"{4:064x}",
    ]
    assert [entry["rank"] for entry in artifact.entries] == [1, 2, 3]
    twin_entry = artifact.entries[1]
    assert twin_entry["family_id"] == f"{1:064x}"
    assert twin_entry["family_members"] == [f"{1:064x}", f"{2:064x}"]
    assert artifact.source["pool"] == 5
    assert artifact.source["gate_passers"] == 4
    assert artifact.source["families_collapsed"] == 1
    assert artifact.source["data_snapshot_hashes"] == ["snapA", "snapB"]


def test_golden_corrupt_artifact_is_detected_and_rebuilds_to_locked_bytes(
    tmp_path: Path,
) -> None:
    """The R42 stamp-detected → deterministic-rebuild golden."""
    path = write_top100(tmp_path, _fixture_artifact())
    pristine = path.read_bytes()
    assert hashlib.sha256(pristine).hexdigest() == _GOLDEN_TOP100_SHA256
    # Torn/tampered artifact: the stamp hash gate refuses it…
    path.write_bytes(pristine[: len(pristine) // 2])
    assert load_top100(tmp_path, _RUN) is None
    # …and the rebuild converges to the IDENTICAL locked bytes.
    write_top100(tmp_path, _fixture_artifact())
    assert path.read_bytes() == pristine
    reloaded = load_top100(tmp_path, _RUN)
    assert reloaded is not None
    assert reloaded.rendered_bytes() == pristine


def test_wired_exec_provider_arm_is_additive_only_vs_the_unwired_golden() -> None:
    """`DEC-OPTIONS-EXEC-FEEDBACK-001` / DEC-FILL-EXEC-GRID-1M-001: the wired
    arm over the SAME frozen fixture differs from the unwired golden ONLY
    additively (the #1749 additive-only-diff pattern — the cascade strip pin
    in test_engine_top100_artifact.py).

    Mechanism: ``_fixture_artifact`` is driven through a capturing wrapper so
    the EXACT candidates + kwargs the golden was built from are re-invoked
    with deterministic providers added — no fixture duplication, no fixture
    file touched, and the captured unwired build is asserted to be the LOCKED
    golden bytes (anti-vacuous). Then: stripping EXACTLY the new keys
    (per-entry ``options_exec`` / ``futures_exec`` / ``fill_sensitivity`` +
    the ``*_wired`` source flags) reproduces the unwired payload byte-for-byte
    under the deterministic JSON law, and every new key is enumerated.
    """
    import json
    from typing import Any
    from unittest import mock

    from brutex.engine.top100_artifact import Top100Candidate
    from brutex.engine.top100_artifact import build_top100 as _real_build

    captured: dict[str, Any] = {}

    def _capture(candidates: Any, **kwargs: Any) -> Top100Artifact:
        captured["candidates"] = list(candidates)
        captured["kwargs"] = dict(kwargs)
        return _real_build(candidates, **kwargs)

    with mock.patch(f"{__name__}.build_top100", _capture):
        unwired = _fixture_artifact()
    # Anti-vacuous: the captured rebuild IS the locked golden bytes.
    assert hashlib.sha256(unwired.rendered_bytes()).hexdigest() == _GOLDEN_TOP100_SHA256

    # Deterministic providers — pure functions of the candidate's run_id.
    def _options(c: Top100Candidate) -> dict[str, object]:
        return {
            "status": "priced",
            "net_after_cost_paisa": 10_000 + int(c.row.run_id, 16),
            "window": "2024-10-01..",
        }

    def _futures(c: Top100Candidate) -> dict[str, object]:
        return {"status": "not_priced", "reason": f"deferred:{c.row.run_id[-2:]}"}

    def _fill(c: Top100Candidate) -> dict[str, object]:
        return {"status": "priced", "net_delta_paisa": -int(c.row.run_id, 16)}

    wired = _real_build(
        captured["candidates"],
        **captured["kwargs"],
        options_exec_provider=_options,
        futures_exec_provider=_futures,
        fill_sensitivity_provider=_fill,
    )
    unwired_payload = unwired.to_payload()
    wired_payload = wired.to_payload()

    # The additive delta, ENUMERATED exactly — root level: no new root keys.
    assert set(wired_payload) == set(unwired_payload)
    # Source: exactly the three emit-on-wired flags, all True.
    new_source_flags = {"options_exec_wired", "futures_exec_wired", "fill_sensitivity_wired"}
    wired_source = wired_payload["source"]
    unwired_source = unwired_payload["source"]
    assert isinstance(wired_source, dict)
    assert isinstance(unwired_source, dict)
    assert set(wired_source) - set(unwired_source) == new_source_flags
    assert all(wired_source[flag] is True for flag in new_source_flags)
    # Entries: same count + order; each entry gains EXACTLY the three keys,
    # carrying the deterministic providers' objects verbatim.
    new_entry_keys = {"options_exec", "futures_exec", "fill_sensitivity"}
    wired_entries = wired_payload["entries"]
    unwired_entries = unwired_payload["entries"]
    assert isinstance(wired_entries, list)
    assert isinstance(unwired_entries, list)
    assert len(wired_entries) == len(unwired_entries) == 3
    ranked_ids = [f"{3:064x}", f"{1:064x}", f"{4:064x}"]  # the locked golden order
    assert [entry["run_id"] for entry in wired_entries] == ranked_ids
    for entry, unwired_entry in zip(wired_entries, unwired_entries, strict=True):
        run_id = entry["run_id"]
        assert isinstance(run_id, str)
        assert set(entry) - set(unwired_entry) == new_entry_keys
        assert entry["options_exec"] == {
            "status": "priced",
            "net_after_cost_paisa": 10_000 + int(run_id, 16),
            "window": "2024-10-01..",
        }
        assert entry["futures_exec"] == {
            "status": "not_priced",
            "reason": f"deferred:{run_id[-2:]}",
        }
        assert entry["fill_sensitivity"] == {
            "status": "priced",
            "net_delta_paisa": -int(run_id, 16),
        }

    # Every PRE-EXISTING key/value byte-identical: stripping EXACTLY the new
    # keys reproduces the unwired golden payload under the deterministic JSON
    # rendering law (sorted, compact — the artifact's own byte law).
    stripped = dict(wired_payload)
    stripped["source"] = {k: v for k, v in wired_source.items() if k not in new_source_flags}
    stripped["entries"] = [
        {k: v for k, v in entry.items() if k not in new_entry_keys} for entry in wired_entries
    ]

    def _dumps_law(payload: object) -> str:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )

    assert _dumps_law(stripped) == _dumps_law(unwired_payload)
