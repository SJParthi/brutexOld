"""Unit tests — :mod:`brutex.engine.run_verdicts` (G8, DEC-SWEEP-UNKILLABLE-CORE-001).

The per-mask stage3/cascade verdict fold matrix (passed/rejected/not_run;
missing inputs fold to the HONEST ``not_run`` / an empty mask universe — never
fabricated), the contract fixture pinning the exact ``verdicts.json`` shape
(design §3), the R42 stamp sidecar, and the never-500 tolerant loader.

Manifest spec dicts are produced by the REAL
:func:`brutex.engine.cascade_wire._spec_to_dict` /
:func:`brutex.engine.cascade_wire._stage3_to_dict` serialisers so a drift in
the manifest shape breaks THESE tests, not production.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import pytest
from brutex.engine.cascade_wire import (
    CascadeTupleSpec,
    Stage3CellRecord,
    Stage3ManifestRecord,
    _spec_to_dict,  # pyright: ignore[reportPrivateUsage]
    _stage3_to_dict,  # pyright: ignore[reportPrivateUsage]
    manifest_path,
    report_path,
)
from brutex.engine.run_verdicts import (
    CASCADE_PASSED,
    CASCADE_REJECTED,
    NOT_RUN,
    STAGE3_REFINED,
    STAGE3_UNGATED,
    VERDICTS_FILENAME,
    VERDICTS_SCHEMA_VERSION,
    VERDICTS_STAGE,
    build_run_verdicts,
    load_run_verdicts,
    verdicts_path,
)
from brutex.observability.stage_stamp import StampVerdict, validate_stage_stamp
from structlog.testing import capture_logs

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_g8_verdict_fold_matrix": ("G8",),
    "test_g8_missing_report_folds_all_cascade_not_run": ("G8",),
    "test_g8_missing_manifest_yields_empty_universe": ("G8",),
    "test_g8_contract_fixture_pins_verdicts_json_shape": ("G8", "R42"),
    "test_g8_corrupt_manifest_json_warns_and_degrades": ("G8",),
    "test_g8_malformed_manifest_structure_warns_and_degrades": ("G8",),
    "test_g8_malformed_report_structure_folds_not_run": ("G8",),
    "test_g8_non_object_report_document_folds_not_run": ("G8",),
    "test_g8_unmapped_report_candidate_counted_never_guessed": ("G8",),
    "test_g8_stage3_disabled_folds_stage3_not_run": ("G8",),
    "test_g8_any_pass_fold_across_candidates_and_groups": ("G8",),
    "test_g8_mask_hex_is_lowercase_unprefixed": ("G8",),
    "test_g8_load_missing_returns_empty_silently": ("G8",),
    "test_g8_load_corrupt_returns_empty_with_warn": ("G8",),
    "test_g8_load_foreign_run_id_returns_empty_with_warn": ("G8",),
    "test_g8_load_malformed_shape_returns_empty_with_warn": ("G8",),
    "test_g8_load_non_object_document_returns_empty_with_warn": ("G8",),
    "test_g8_load_roundtrip_exact": ("G8",),
}

#: A deterministic 64-hex run identity for the run under test.
RUN_ID: Final[str] = "f" * 64


def _winner(seed: str, mask: int) -> tuple[str, int]:
    """One (run_id, mask) winner pair with a canonical 64-hex lowercase id."""
    return (seed * (64 // len(seed)) + seed[: 64 % len(seed)], mask)


def _spec(
    *,
    winners: tuple[tuple[str, int], ...],
    sl_paisa: int | None = 1_000,
    tp_paisa: int | None = 2_000,
    proximity: tuple[int, int, int] | None = (10, 20, 30),
    tf_label: str = "5m",
) -> dict[str, object]:
    """One manifest spec dict rendered by the REAL serialiser."""
    return _spec_to_dict(
        CascadeTupleSpec(
            instrument="NSE-NIFTY",
            tf_label=tf_label,
            tf_minutes=5,
            direction_label="bull",
            sl_paisa=sl_paisa,
            tp_paisa=tp_paisa,
            prior_n=3,
            proximity_bps=proximity,
            start_equity_paisa=10_000_000,
            winners=winners,
        )
    )


def _stage3_block(
    cells: tuple[Stage3CellRecord, ...], *, enabled: bool = True
) -> dict[str, object]:
    """One manifest stage3 dict rendered by the REAL serialiser."""
    return _stage3_to_dict(
        Stage3ManifestRecord(
            enabled=enabled,
            train_snapshot_hash="e" * 64 if enabled else None,
            refined_cells=cells,
        )
    )


def _cell(
    *,
    sl_paisa: int = 1_000,
    tp_paisa: int = 2_000,
    proximity: tuple[int, int, int] | None = (10, 20, 30),
    tf_label: str = "5m",
) -> Stage3CellRecord:
    """One refined-cell record whose coordinates match :func:`_spec` defaults."""
    return Stage3CellRecord(
        instrument="NSE-NIFTY",
        tf_label=tf_label,
        direction_label="bull",
        k=3,
        sl_paisa=sl_paisa,
        tp_paisa=tp_paisa,
        proximity_bps=proximity,
    )


def _write_manifest(results_root: Path, doc: object) -> None:
    path = manifest_path(results_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _write_report(results_root: Path, verdicts: list[tuple[str, bool]]) -> None:
    """Write a cascade-report.json with one group carrying *verdicts*."""
    doc = {
        "version": 1,
        "sweep_id": RUN_ID,
        "n_input": len(verdicts),
        "n_survived": sum(1 for _, passed in verdicts if passed),
        "groups": [
            {
                "instrument": "NSE-NIFTY",
                "tf": "5m",
                "report": {
                    "verdicts": [
                        {"candidate_id": candidate_id, "passed": passed}
                        for candidate_id, passed in verdicts
                    ]
                },
            }
        ],
    }
    path = report_path(results_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


# ────────────────────────────────────────────────────────────────────────
# The fold matrix
# ────────────────────────────────────────────────────────────────────────


def test_g8_verdict_fold_matrix(tmp_path: Path) -> None:
    """Every (stage3 × cascade) verdict combination folds exactly as specified."""
    # Stage-2 spec: mask 1 (passed), mask 2 (rejected), mask 5 (no verdict).
    w1, w2, w5 = _winner("a1", 1), _winner("b2", 2), _winner("c3", 5)
    # Stage-3 refined-cell spec (distinct SL so its coordinates match the cell,
    # not the stage-2 spec): mask 1 again (⇒ refined) + mask 8 (⇒ ungated).
    w1r, w8 = _winner("d4", 1), _winner("e5", 8)
    manifest = {
        "specs": [
            _spec(winners=(w1, w2, w5)),
            _spec(winners=(w1r, w8), sl_paisa=3_000),
        ],
        "stage3": _stage3_block((_cell(sl_paisa=3_000),)),
    }
    _write_manifest(tmp_path, manifest)
    _write_report(
        tmp_path,
        [(w1[0], True), (w2[0], False), (w1r[0], False), (w8[0], False)],
    )

    build_run_verdicts(tmp_path, run_id=RUN_ID)
    by_mask = load_run_verdicts(tmp_path, RUN_ID)

    assert by_mask == {
        # gated + refined; any-pass across its two candidates ⇒ passed.
        "1": {"stage3": STAGE3_REFINED, "cascade": CASCADE_PASSED},
        # gated only; its one candidate was rejected.
        "2": {"stage3": NOT_RUN, "cascade": CASCADE_REJECTED},
        # gated only; the report never judged it ⇒ honest not_run.
        "5": {"stage3": NOT_RUN, "cascade": NOT_RUN},
        # refined-cell only ⇒ UNGATED (the G8 co-rank hazard), rejected.
        "8": {"stage3": STAGE3_UNGATED, "cascade": CASCADE_REJECTED},
    }


def test_g8_missing_report_folds_all_cascade_not_run(tmp_path: Path) -> None:
    """No cascade-report.json ⇒ EVERY mask's cascade verdict is the honest not_run."""
    _write_manifest(
        tmp_path,
        {
            "specs": [_spec(winners=(_winner("a1", 3),), proximity=None, sl_paisa=None)],
            "stage3": None,
        },
    )
    build_run_verdicts(tmp_path, run_id=RUN_ID)
    assert load_run_verdicts(tmp_path, RUN_ID) == {"3": {"stage3": NOT_RUN, "cascade": NOT_RUN}}


def test_g8_missing_manifest_yields_empty_universe(tmp_path: Path) -> None:
    """No manifest ⇒ the mask universe is unknowable — an EMPTY by_mask, never invented."""
    _write_report(tmp_path, [("9" * 64, True)])
    with capture_logs() as logs:
        build_run_verdicts(tmp_path, run_id=RUN_ID)
    assert load_run_verdicts(tmp_path, RUN_ID) == {}
    # The report candidate could not be mask-mapped without a manifest — and
    # with NO manifest facts at all the fold reports an empty universe without
    # inventing an unmapped tally either.
    assert all(entry["event"] != "run_verdicts.unmapped_candidates" for entry in logs)


def test_g8_contract_fixture_pins_verdicts_json_shape(tmp_path: Path) -> None:
    """The artifact's exact JSON document + R42 stamp sidecar are contract-pinned."""
    _write_manifest(
        tmp_path,
        {"specs": [_spec(winners=(_winner("a1", 255),))], "stage3": None},
    )
    _write_report(tmp_path, [(_winner("a1", 255)[0], True)])

    dest = build_run_verdicts(tmp_path, run_id=RUN_ID)

    assert dest == verdicts_path(tmp_path, RUN_ID)
    assert dest == tmp_path / "runs" / RUN_ID / VERDICTS_FILENAME
    doc = json.loads(dest.read_text(encoding="utf-8"))
    # Top-level contract shape — design §3, byte-level key set pinned.
    assert doc == {
        "run_id": RUN_ID,
        "by_mask": {"ff": {"stage3": "not_run", "cascade": "passed"}},
        "schema_version": VERDICTS_SCHEMA_VERSION,
    }
    assert VERDICTS_SCHEMA_VERSION == 1
    # R42: the artifact is stamped, and the stamp validates OK against the run.
    verdict, stamp = validate_stage_stamp(
        dest, expected_stage=VERDICTS_STAGE, expected_run_id=RUN_ID
    )
    assert verdict is StampVerdict.OK
    assert stamp is not None
    assert stamp.stage == "verdicts"
    assert stamp.run_id == RUN_ID


def test_g8_corrupt_manifest_json_warns_and_degrades(tmp_path: Path) -> None:
    """Garbage manifest bytes ⇒ one input_unreadable WARN + an empty universe."""
    path = manifest_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00not json")
    with capture_logs() as logs:
        build_run_verdicts(tmp_path, run_id=RUN_ID)
    events = [entry["event"] for entry in logs]
    assert "run_verdicts.input_unreadable" in events
    assert load_run_verdicts(tmp_path, RUN_ID) == {}


def test_g8_malformed_manifest_structure_warns_and_degrades(tmp_path: Path) -> None:
    """Valid JSON with a wrong structure ⇒ manifest_malformed WARN + empty universe."""
    for bad in (["not", "an", "object"], {"specs": [{"winners": [["x", 1]]}]}):
        _write_manifest(tmp_path, bad)
        with capture_logs() as logs:
            build_run_verdicts(tmp_path, run_id=RUN_ID)
        assert any(entry["event"] == "run_verdicts.manifest_malformed" for entry in logs)
        assert load_run_verdicts(tmp_path, RUN_ID) == {}


def test_g8_malformed_report_structure_folds_not_run(tmp_path: Path) -> None:
    """A structurally-broken report ⇒ report_malformed WARN + all cascade not_run."""
    _write_manifest(tmp_path, {"specs": [_spec(winners=(_winner("a1", 4),))], "stage3": None})
    path = report_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"groups": [{"report": {}}]}), encoding="utf-8")
    with capture_logs() as logs:
        build_run_verdicts(tmp_path, run_id=RUN_ID)
    assert any(entry["event"] == "run_verdicts.report_malformed" for entry in logs)
    assert load_run_verdicts(tmp_path, RUN_ID) == {"4": {"stage3": NOT_RUN, "cascade": NOT_RUN}}


def test_g8_non_object_report_document_folds_not_run(tmp_path: Path) -> None:
    """A non-object report document (JSON array) hits the typed TypeError arm."""
    _write_manifest(tmp_path, {"specs": [_spec(winners=(_winner("a1", 4),))], "stage3": None})
    path = report_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2]", encoding="utf-8")
    with capture_logs() as logs:
        build_run_verdicts(tmp_path, run_id=RUN_ID)
    assert any(entry["event"] == "run_verdicts.report_malformed" for entry in logs)
    assert load_run_verdicts(tmp_path, RUN_ID) == {"4": {"stage3": NOT_RUN, "cascade": NOT_RUN}}


def test_g8_unmapped_report_candidate_counted_never_guessed(tmp_path: Path) -> None:
    """A report candidate the manifest does not name is WARN-counted, never mask-guessed."""
    known = _winner("a1", 6)
    _write_manifest(tmp_path, {"specs": [_spec(winners=(known,))], "stage3": None})
    _write_report(tmp_path, [(known[0], False), ("0" * 64, True)])
    with capture_logs() as logs:
        build_run_verdicts(tmp_path, run_id=RUN_ID)
    unmapped = [e for e in logs if e["event"] == "run_verdicts.unmapped_candidates"]
    assert len(unmapped) == 1
    assert unmapped[0]["count"] == 1
    # The foreign candidate's `passed=True` never leaked into mask 6's verdict.
    assert load_run_verdicts(tmp_path, RUN_ID) == {
        "6": {"stage3": NOT_RUN, "cascade": CASCADE_REJECTED}
    }


def test_g8_stage3_disabled_folds_stage3_not_run(tmp_path: Path) -> None:
    """An enabled=false stage3 block ⇒ stage3 is not_run even for cell-shaped specs."""
    w = _winner("a1", 9)
    _write_manifest(
        tmp_path,
        {"specs": [_spec(winners=(w,))], "stage3": _stage3_block((), enabled=False)},
    )
    build_run_verdicts(tmp_path, run_id=RUN_ID)
    assert load_run_verdicts(tmp_path, RUN_ID) == {"9": {"stage3": NOT_RUN, "cascade": NOT_RUN}}


def test_g8_any_pass_fold_across_candidates_and_groups(tmp_path: Path) -> None:
    """A mask with one rejected + one passed candidate is PASSED (any-pass fold)."""
    wa, wb = _winner("a1", 7), _winner("b2", 7)
    _write_manifest(tmp_path, {"specs": [_spec(winners=(wa, wb))], "stage3": None})
    # The SAME candidate id also appears twice (two groups): False then True —
    # the candidate-level fold keeps its best outcome.
    doc = {
        "groups": [
            {"report": {"verdicts": [{"candidate_id": wa[0], "passed": False}]}},
            {
                "report": {
                    "verdicts": [
                        {"candidate_id": wb[0], "passed": False},
                        {"candidate_id": wb[0], "passed": True},
                    ]
                }
            },
        ]
    }
    path = report_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    build_run_verdicts(tmp_path, run_id=RUN_ID)
    assert load_run_verdicts(tmp_path, RUN_ID) == {
        "7": {"stage3": NOT_RUN, "cascade": CASCADE_PASSED}
    }


def test_g8_mask_hex_is_lowercase_unprefixed(tmp_path: Path) -> None:
    """Mask keys are lowercase hex without 0x / padding — 255 ⇒ 'ff', 10 ⇒ 'a'."""
    _write_manifest(
        tmp_path,
        {"specs": [_spec(winners=(_winner("a1", 255), _winner("b2", 10)))], "stage3": None},
    )
    build_run_verdicts(tmp_path, run_id=RUN_ID)
    assert sorted(load_run_verdicts(tmp_path, RUN_ID)) == ["a", "ff"]


# ────────────────────────────────────────────────────────────────────────
# load_run_verdicts — the never-500 tolerant reader
# ────────────────────────────────────────────────────────────────────────


def test_g8_load_missing_returns_empty_silently(tmp_path: Path) -> None:
    with capture_logs() as logs:
        assert load_run_verdicts(tmp_path, RUN_ID) == {}
    assert logs == []


def test_g8_load_corrupt_returns_empty_with_warn(tmp_path: Path) -> None:
    dest = verdicts_path(tmp_path, RUN_ID)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"{broken")
    with capture_logs() as logs:
        assert load_run_verdicts(tmp_path, RUN_ID) == {}
    assert any(entry["event"] == "run_verdicts.input_unreadable" for entry in logs)


def test_g8_load_foreign_run_id_returns_empty_with_warn(tmp_path: Path) -> None:
    """A swapped-in verdicts file for a DIFFERENT run is refused, never trusted."""
    dest = verdicts_path(tmp_path, RUN_ID)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"run_id": "0" * 64, "by_mask": {}, "schema_version": 1}),
        encoding="utf-8",
    )
    with capture_logs() as logs:
        assert load_run_verdicts(tmp_path, RUN_ID) == {}
    assert any(entry["event"] == "run_verdicts.unreadable" for entry in logs)


def test_g8_load_malformed_shape_returns_empty_with_warn(tmp_path: Path) -> None:
    dest = verdicts_path(tmp_path, RUN_ID)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"run_id": RUN_ID, "by_mask": {"1": {"stage3": "refined"}}}),
        encoding="utf-8",
    )
    with capture_logs() as logs:
        assert load_run_verdicts(tmp_path, RUN_ID) == {}
    assert any(entry["event"] == "run_verdicts.unreadable" for entry in logs)


def test_g8_load_non_object_document_returns_empty_with_warn(tmp_path: Path) -> None:
    dest = verdicts_path(tmp_path, RUN_ID)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("[]", encoding="utf-8")
    with capture_logs() as logs:
        assert load_run_verdicts(tmp_path, RUN_ID) == {}
    assert any(entry["event"] == "run_verdicts.unreadable" for entry in logs)


def test_g8_load_roundtrip_exact(tmp_path: Path) -> None:
    """A valid artifact round-trips its by_mask map exactly (mutation-hardened)."""
    dest = verdicts_path(tmp_path, RUN_ID)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": RUN_ID,
        "by_mask": {
            "1f": {"stage3": "ungated", "cascade": "rejected"},
            "2": {"stage3": "refined", "cascade": "passed"},
        },
        "schema_version": 1,
    }
    dest.write_text(json.dumps(payload), encoding="utf-8")
    assert load_run_verdicts(tmp_path, RUN_ID) == payload["by_mask"]


def test_g8_verdict_vocabulary_constants() -> None:
    """The frozen §2 vocabulary values — exact strings, mutation-hardened."""
    assert STAGE3_REFINED == "refined"
    assert STAGE3_UNGATED == "ungated"
    assert CASCADE_PASSED == "passed"
    assert CASCADE_REJECTED == "rejected"
    assert NOT_RUN == "not_run"
    assert VERDICTS_FILENAME == "verdicts.json"
    assert VERDICTS_STAGE == "verdicts"


TRACEABILITY["test_g8_verdict_vocabulary_constants"] = ("G8",)


def test_g8_build_write_failure_propagates(tmp_path: Path) -> None:
    """A write failure raises (the run-core exit path owns fail-softness)."""
    blocker = tmp_path / "runs"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        build_run_verdicts(tmp_path, run_id=RUN_ID)


TRACEABILITY["test_g8_build_write_failure_propagates"] = ("G8",)


def test_g8_legacy_manifest_without_fib_anchor_matches(tmp_path: Path) -> None:
    """A pre-fib-axis manifest (no fib_anchor keys) still coordinate-matches cells."""
    w = _winner("a1", 12)
    spec = _spec(winners=(w,))
    del spec["fib_anchor"]
    stage3 = _stage3_block((_cell(),))
    cells = cast("list[dict[str, object]]", stage3["refined_cells"])
    for cell in cells:
        del cell["fib_anchor"]
    _write_manifest(tmp_path, {"specs": [spec], "stage3": stage3})
    build_run_verdicts(tmp_path, run_id=RUN_ID)
    # The spec matched the refined cell (both default to prev_day) and the
    # mask appears ONLY there ⇒ ungated.
    assert load_run_verdicts(tmp_path, RUN_ID) == {
        "c": {"stage3": STAGE3_UNGATED, "cascade": NOT_RUN}
    }


TRACEABILITY["test_g8_legacy_manifest_without_fib_anchor_matches"] = ("G8",)
