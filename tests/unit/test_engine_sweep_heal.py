"""Unit tests for the quarantine heal core (`brutex.engine.sweep_heal`).

100% line + branch coverage of DEC-SWEEP-HEAL-001: every plan/entry
validation arm, plan (de)serialization arms, `build_heal_plan` over a REAL
recount report (incl. the C10 non-grid refusal), every `assess_heal` decision
arm (fresh / stale / partial / already-quarantined / mass threshold /
running-sweep), and `execute_heal` with injected FS primitives pinning the
crash windows, idempotence, and THE heal-then-resweep regression.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.sweep_heal import (
    ARTIFACT_SUFFIXES,
    HealAssessment,
    HealEntry,
    HealPlan,
    SweepHealError,
    assess_heal,
    build_heal_plan,
    execute_heal,
    read_heal_plan,
    render_heal_plan,
    write_heal_plan,
)
from brutex.engine.sweep_orchestrator import make_store_for, run_sweep_grid
from brutex.engine.sweep_run import ComboDedup
from brutex.engine.sweep_verify import (
    QUARANTINE_DIRNAME,
    RecordedContext,
    TreeVerifyReport,
    VerifyFinding,
    compare_recount,
    plan_expected_stores,
    verify_identity,
    verify_structural,
)
from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain

# Reuse the doctor suite's real-sweep fixtures (one source of bars/grid truth).
from tests.unit.test_engine_sweep_verify import _grid, _store_path, _sweep, _trending_bars

_CTX = RecordedContext(
    data_snapshot_hash="snap0001", indicator_version="ind-v1", code_commit_sha="deadbeef"
)
_GID = "ab" * 32


def _entry(**overrides: object) -> HealEntry:
    base: dict[str, object] = {
        "rel_store": "NIFTY/1minute/BUY_k1_" + "ab12cd34ef56ab12.parquet",
        "tuple_digest": "ab12cd34ef56ab12",
        "artifact_hashes": {"": "0f" * 32, ".trials.json": "1f" * 32, ".complete.json": None},
    }
    base.update(overrides)
    return HealEntry(**base)  # type: ignore[arg-type]


def _plan(**overrides: object) -> HealPlan:
    base: dict[str, object] = {
        "created_utc": "20260611T103000Z",
        "grid_identity": _GID,
        "context": _CTX,
        "n_expected_stores": 10,
        "entries": (_entry(),),
    }
    base.update(overrides)
    return HealPlan(**base)  # type: ignore[arg-type]


# ── Validation arms ──────────────────────────────────────────────────────────


class TestEntryValidation:
    def test_valid_constructs(self) -> None:
        assert _entry().artifact_hashes[""] is not None

    @pytest.mark.parametrize("rel", ["", "/abs/path.parquet"])
    def test_bad_rel_store_raises(self, rel: str) -> None:
        with pytest.raises(SweepHealError, match="relative path"):
            _entry(rel_store=rel)

    def test_quarantine_path_refused(self) -> None:
        with pytest.raises(SweepHealError, match="quarantine"):
            _entry(rel_store=f"{QUARANTINE_DIRNAME}/x/y.parquet")

    def test_bad_digest_raises(self) -> None:
        with pytest.raises(SweepHealError, match="tuple_digest"):
            _entry(tuple_digest="nope")

    def test_wrong_hash_keys_raise(self) -> None:
        with pytest.raises(SweepHealError, match="keys must be exactly"):
            _entry(artifact_hashes={"": "0f" * 32})

    def test_missing_store_hash_raises(self) -> None:
        hashes = {"": None, ".trials.json": None, ".complete.json": None}
        with pytest.raises(SweepHealError, match="store hash is mandatory"):
            _entry(artifact_hashes=hashes)

    def test_bad_hash_value_raises(self) -> None:
        hashes = {"": "zz" * 32, ".trials.json": None, ".complete.json": None}
        with pytest.raises(SweepHealError, match="not valid hex"):
            _entry(artifact_hashes=hashes)


class TestPlanValidation:
    def test_valid_constructs_and_subdir(self) -> None:
        plan = _plan()
        assert plan.quarantine_subdir == f"20260611T103000Z-{_GID[:8]}"

    @pytest.mark.parametrize("stamp", ["", "has/slash", "has space"])
    def test_unsafe_stamp_raises(self, stamp: str) -> None:
        with pytest.raises(SweepHealError, match="filesystem-safe"):
            _plan(created_utc=stamp)

    def test_bad_grid_identity_raises(self) -> None:
        with pytest.raises(SweepHealError, match="grid_identity"):
            _plan(grid_identity="short")

    def test_n_expected_below_one_raises(self) -> None:
        with pytest.raises(SweepHealError, match="n_expected_stores"):
            _plan(n_expected_stores=0)

    def test_unsorted_entries_raise(self) -> None:
        a = _entry(rel_store="b/x.parquet")
        b = _entry(rel_store="a/x.parquet")
        with pytest.raises(SweepHealError, match="sorted.*unique"):
            _plan(entries=(a, b))

    def test_duplicate_entries_raise(self) -> None:
        with pytest.raises(SweepHealError, match="sorted.*unique"):
            _plan(entries=(_entry(), _entry()))


# ── Serialization ────────────────────────────────────────────────────────────


class TestPlanSerialization:
    def test_render_deterministic_and_round_trip(self, tmp_path: Path) -> None:
        plan = _plan()
        assert render_heal_plan(plan) == render_heal_plan(plan)
        path = write_heal_plan(tmp_path, plan)
        assert path.parent.name == "heal"
        assert read_heal_plan(path) == plan

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SweepHealError, match="not found"):
            read_heal_plan(tmp_path / "nope.json")

    def test_corrupt_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text("{corrupt", encoding="utf-8")
        with pytest.raises(SweepHealError, match="not valid JSON"):
            read_heal_plan(path)

    def test_non_object_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text("[1]", encoding="utf-8")
        with pytest.raises(SweepHealError, match="JSON object"):
            read_heal_plan(path)

    def test_wrong_schema_version_raises(self, tmp_path: Path) -> None:
        payload = json.loads(render_heal_plan(_plan()))
        payload["schema_version"] = 99
        path = tmp_path / "p.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SweepHealError, match="unsupported"):
            read_heal_plan(path)

    def test_malformed_field_raises(self, tmp_path: Path) -> None:
        payload = json.loads(render_heal_plan(_plan()))
        del payload["entries"][0]["rel_store"]
        path = tmp_path / "p.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SweepHealError, match="malformed field"):
            read_heal_plan(path)


# ── build_heal_plan over a REAL recount ──────────────────────────────────────


def _condemned_tree(tmp_path: Path) -> tuple[Path, Path, object, TreeVerifyReport]:
    """Sweep twice, tamper the original store, recount → a real CORRUPT report."""
    grid = _grid()
    original, scratch = tmp_path / "original", tmp_path / "scratch"
    for root in (original, scratch):
        root.mkdir(parents=True)
        _sweep(root, grid)
    store = _store_path(original, grid)
    store.write_bytes(store.read_bytes() + b"tampered")
    plan_map = plan_expected_stores(original, grid)
    report = compare_recount(original, scratch, plan_map, context_match=True)
    assert not report.clean
    return original, scratch, grid, report


class TestBuildHealPlan:
    def test_builds_from_real_recount(self, tmp_path: Path) -> None:
        original, _, grid, report = _condemned_tree(tmp_path)
        plan_map = plan_expected_stores(original, grid)  # type: ignore[arg-type]
        plan = build_heal_plan(
            report,
            plan_map,
            root=original,
            grid_identity=_GID,
            context=_CTX,
            created_utc="20260611T103000Z",
        )
        assert plan.n_expected_stores == 1
        (entry,) = plan.entries
        store = _store_path(original, grid)  # type: ignore[arg-type]
        assert entry.rel_store == store.relative_to(original).as_posix()
        assert entry.tuple_digest == store.stem.split("_")[-1]
        # The frozen hashes ARE the (tampered) condemnation-time bytes.
        assert entry.artifact_hashes[""] == hashlib.sha256(store.read_bytes()).hexdigest()

    def test_non_recount_report_refused(self, tmp_path: Path) -> None:
        report = TreeVerifyReport(root="r", tier="identity", checked_stores=0, findings=())
        with pytest.raises(SweepHealError, match="recount report"):
            build_heal_plan(
                report, {}, root=tmp_path, grid_identity=_GID, context=_CTX, created_utc="x1"
            )

    def test_non_grid_path_refused(self, tmp_path: Path) -> None:
        # C10: a condemned path not attributable to a grid tuple refuses.
        finding = VerifyFinding(
            path="NIFTY/1minute/foreign.parquet",
            code="recount_divergence",
            severity="corrupt",
            detail="d",
        )
        report = TreeVerifyReport(root="r", tier="recount", checked_stores=1, findings=(finding,))
        with pytest.raises(SweepHealError, match="not healable"):
            build_heal_plan(
                report, {}, root=tmp_path, grid_identity=_GID, context=_CTX, created_utc="x1"
            )

    def test_vanished_store_refused(self, tmp_path: Path) -> None:
        original, _, grid, report = _condemned_tree(tmp_path)
        plan_map = plan_expected_stores(original, grid)  # type: ignore[arg-type]
        _store_path(original, grid).unlink()  # type: ignore[arg-type]
        with pytest.raises(SweepHealError, match="vanished"):
            build_heal_plan(
                report,
                plan_map,
                root=original,
                grid_identity=_GID,
                context=_CTX,
                created_utc="x1",
            )

    def test_non_divergence_findings_ignored(self, tmp_path: Path) -> None:
        original, _, grid, _report = _condemned_tree(tmp_path)
        plan_map = plan_expected_stores(original, grid)  # type: ignore[arg-type]
        warn = VerifyFinding(
            path="x.parquet", code="recount_scratch_absent", severity="warn", detail="d"
        )
        report = TreeVerifyReport(root="r", tier="recount", checked_stores=1, findings=(warn,))
        plan = build_heal_plan(
            report, plan_map, root=original, grid_identity=_GID, context=_CTX, created_utc="x1"
        )
        assert plan.entries == ()

    def test_vix_divergence_findings_are_never_heal_grade(self, tmp_path: Path) -> None:
        """DEC-VIX-STAMP-WIRE-001 round-2: the VIX-only recount family — the
        India-VIX lake moved between run and recount, a REFERENCE-ONLY
        annotation drift — must never condemn a tuple (the heal consumes
        exactly ``recount_divergence``; the VIX codes are not aliases)."""
        original, _, grid, _report = _condemned_tree(tmp_path)
        plan_map = plan_expected_stores(original, grid)  # type: ignore[arg-type]
        findings = tuple(
            VerifyFinding(path="x.parquet", code=code, severity="warn", detail="d")
            for code in ("recount_vix_divergence", "cell_recount_vix_divergence")
        )
        report = TreeVerifyReport(root="r", tier="recount", checked_stores=1, findings=findings)
        plan = build_heal_plan(
            report, plan_map, root=original, grid_identity=_GID, context=_CTX, created_utc="x1"
        )
        assert plan.entries == ()


# ── assess_heal arms ─────────────────────────────────────────────────────────


def _real_plan(tmp_path: Path) -> tuple[Path, HealPlan]:
    original, _, grid, report = _condemned_tree(tmp_path)
    plan_map = plan_expected_stores(original, grid)  # type: ignore[arg-type]
    plan = build_heal_plan(
        report,
        plan_map,
        root=original,
        grid_identity=_GID,
        context=_CTX,
        created_utc="20260611T103000Z",
    )
    return original, plan


class TestAssessHeal:
    def test_missing_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SweepHealError, match="not found"):
            assess_heal(tmp_path / "nope", _plan(), sweep_running=False)

    def test_fresh_evidence_moves(self, tmp_path: Path) -> None:
        root, plan = _real_plan(tmp_path)
        assessment = assess_heal(root, plan, sweep_running=False, allow_mass_quarantine=True)
        assert assessment.ok
        assert [a.decision for a in assessment.actions] == ["move_trio"]
        rendered = assessment.render(root, plan)
        assert "[MOVE_TRIO]" in rendered
        assert "wipe: dedup-cache, checkpoint" in rendered

    def test_running_sweep_refuses(self, tmp_path: Path) -> None:
        root, plan = _real_plan(tmp_path)
        assessment = assess_heal(root, plan, sweep_running=True, allow_mass_quarantine=True)
        assert not assessment.ok
        assert any("RUNNING" in r for r in assessment.refusals)
        assert "REFUSED" in assessment.render(root, plan)

    def test_mass_quarantine_refuses_without_override(self, tmp_path: Path) -> None:
        # The single-tuple tree: 1 of 1 condemned == 100% >= the threshold (C7).
        root, plan = _real_plan(tmp_path)
        assert plan.n_expected_stores == 1
        refused = assess_heal(root, plan, sweep_running=False)
        assert not refused.ok
        assert any("--allow-mass-quarantine" in r for r in refused.refusals)

    def test_mass_quarantine_override(self, tmp_path: Path) -> None:
        root, plan = _real_plan(tmp_path)
        assessment = assess_heal(root, plan, sweep_running=False, allow_mass_quarantine=True)
        assert assessment.ok

    def test_stale_store_refuses_whole_heal(self, tmp_path: Path) -> None:
        root, plan = _real_plan(tmp_path)
        store = root / plan.entries[0].rel_store
        store.write_bytes(store.read_bytes() + b"changed-again")
        assessment = assess_heal(root, plan, sweep_running=False, allow_mass_quarantine=True)
        assert not assessment.ok
        assert any("changed since the report" in r for r in assessment.refusals)

    def test_partial_move_completes(self, tmp_path: Path) -> None:
        # W2 crash shape: store already moved, matching sidecar/sentinel remain.
        root, plan = _real_plan(tmp_path)
        entry = plan.entries[0]
        store = root / entry.rel_store
        dest = root / QUARANTINE_DIRNAME / plan.quarantine_subdir / entry.rel_store
        dest.parent.mkdir(parents=True)
        store.rename(dest)
        assessment = assess_heal(root, plan, sweep_running=False, allow_mass_quarantine=True)
        assert assessment.ok
        assert [a.decision for a in assessment.actions] == ["complete_partial"]

    def test_already_quarantined_is_noop(self, tmp_path: Path) -> None:
        root, plan = _real_plan(tmp_path)
        entry = plan.entries[0]
        store = root / entry.rel_store
        dest = root / QUARANTINE_DIRNAME / plan.quarantine_subdir / entry.rel_store
        dest.parent.mkdir(parents=True)
        for suffix in ARTIFACT_SUFFIXES:
            src = store.with_name(store.name + suffix)
            if src.exists():
                src.rename(dest.with_name(dest.name + suffix))
        assessment = assess_heal(root, plan, sweep_running=False, allow_mass_quarantine=True)
        assert assessment.ok
        assert [a.decision for a in assessment.actions] == ["already_quarantined"]

    def test_mismatched_leftovers_refuse(self, tmp_path: Path) -> None:
        # Store moved, but a leftover sidecar was rewritten since → refuse.
        root, plan = _real_plan(tmp_path)
        entry = plan.entries[0]
        store = root / entry.rel_store
        dest = root / QUARANTINE_DIRNAME / plan.quarantine_subdir / entry.rel_store
        dest.parent.mkdir(parents=True)
        store.rename(dest)
        sidecar = store.with_name(store.name + ".trials.json")
        sidecar.write_bytes(b"{rewritten}")
        assessment = assess_heal(root, plan, sweep_running=False, allow_mass_quarantine=True)
        assert not assessment.ok


# ── execute_heal — crash windows, idempotence, THE regression ────────────────


def _audit(root: Path) -> Path:
    path = root / "audit" / "sweep-all.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestExecuteHeal:
    def _execute(self, root: Path, plan: HealPlan, **kwargs: object) -> object:
        assessment = assess_heal(root, plan, sweep_running=False, allow_mass_quarantine=True)
        assert assessment.ok
        audit_path = _audit(root)
        with AuditWriter.open(audit_path, resume=audit_path.exists()) as audit:
            return execute_heal(root, plan, assessment, audit=audit, **kwargs)  # type: ignore[arg-type]

    def test_refused_assessment_raises(self, tmp_path: Path) -> None:
        root, plan = _real_plan(tmp_path)
        refused = HealAssessment(refusals=("nope",), actions=())
        with (
            AuditWriter.open(_audit(root)) as audit,
            pytest.raises(SweepHealError, match="refused assessment"),
        ):
            execute_heal(root, plan, refused, audit=audit)

    def test_escaping_wipe_target_refused(self, tmp_path: Path) -> None:
        # The containment guard's escape arm, exercised directly (a non-symlink
        # target can only escape via exotic mounts — the guard still refuses).
        from brutex.engine.sweep_heal import _assert_contained

        (tmp_path / "root").mkdir()
        (tmp_path / "outside").mkdir()
        with pytest.raises(SweepHealError, match="outside the results root"):
            _assert_contained(tmp_path / "root", tmp_path / "outside")

    def test_full_heal_moves_trio_and_wipes(self, tmp_path: Path) -> None:
        root, plan = _real_plan(tmp_path)
        (root / "dedup-cache").mkdir()
        (root / "checkpoint").mkdir()
        report = self._execute(root, plan)
        assert report.moved == 1  # type: ignore[attr-defined]
        assert report.wiped == ("dedup-cache", "checkpoint")  # type: ignore[attr-defined]
        entry = plan.entries[0]
        assert not (root / entry.rel_store).exists()
        dest = root / QUARANTINE_DIRNAME / plan.quarantine_subdir / entry.rel_store
        assert hashlib.sha256(dest.read_bytes()).hexdigest() == entry.artifact_hashes[""]
        assert not (root / "dedup-cache").exists()
        assert not (root / "checkpoint").exists()
        # The HEAL lines land on the existing chain and it still verifies.
        actions = [e.action for e in read_entries(_audit(root))]
        assert actions[-3:] == ["HEAL_QUARANTINE", "HEAL_WIPE", "HEAL_COMPLETE"]
        assert verify_chain(_audit(root)) == len(actions)

    def test_heal_rerun_is_idempotent(self, tmp_path: Path) -> None:
        root, plan = _real_plan(tmp_path)
        self._execute(root, plan)
        report = self._execute(root, plan)  # second run: nothing left to move
        assert report.already_quarantined == 1  # type: ignore[attr-defined]
        assert report.moved == 0  # type: ignore[attr-defined]

    def test_crash_mid_move_then_rerun_converges(self, tmp_path: Path) -> None:
        # Injected replace dies after the FIRST file (the store) — W2.
        import os

        root, plan = _real_plan(tmp_path)
        calls = {"n": 0}

        def dying_replace(src: str, dst: str) -> None:
            if calls["n"] >= 1:
                raise OSError("simulated crash mid-quarantine")
            os.replace(src, dst)
            calls["n"] += 1

        with pytest.raises(OSError, match="simulated crash"):
            self._execute(root, plan, replace=dying_replace)
        # The store moved; sidecar/sentinel remain — the engine-safe prefix.
        entry = plan.entries[0]
        assert not (root / entry.rel_store).exists()
        report = self._execute(root, plan)  # re-run completes the partial
        assert report.completed_partial == 1  # type: ignore[attr-defined]

    def test_crash_mid_wipe_then_rerun_converges(self, tmp_path: Path) -> None:
        import shutil as _shutil

        root, plan = _real_plan(tmp_path)
        (root / "dedup-cache").mkdir()
        (root / "checkpoint").mkdir()
        calls = {"n": 0}

        def dying_rmtree(path: Path) -> None:
            if calls["n"] >= 1:
                raise OSError("simulated crash mid-wipe")
            _shutil.rmtree(path)
            calls["n"] += 1

        with pytest.raises(OSError, match="simulated crash"):
            self._execute(root, plan, rmtree=dying_rmtree)
        assert not (root / "dedup-cache").exists()  # first target gone (W4)
        assert (root / "checkpoint").exists()
        report = self._execute(root, plan)  # re-run finishes the wipe
        assert report.wiped == ("checkpoint",)  # type: ignore[attr-defined]
        assert not (root / "checkpoint").exists()

    def test_symlinked_wipe_target_refused(self, tmp_path: Path) -> None:
        root, plan = _real_plan(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "dedup-cache").symlink_to(outside)
        with pytest.raises(SweepHealError, match="symlink"):
            self._execute(root, plan)
        assert outside.exists()  # the symlink target was never touched

    def test_heal_then_resweep_redoes_only_condemned(self, tmp_path: Path) -> None:
        # THE regression: after heal, a re-sweep with the same grid produces a
        # tree byte-identical to the never-corrupted control (the scratch).
        grid = _grid()
        original, scratch = tmp_path / "original", tmp_path / "scratch"
        for r in (original, scratch):
            r.mkdir()
            _sweep(r, grid)
        store = _store_path(original, grid)
        store.write_bytes(store.read_bytes() + b"tampered")
        plan_map = plan_expected_stores(original, grid)
        report = compare_recount(original, scratch, plan_map, context_match=True)
        plan = build_heal_plan(
            report,
            plan_map,
            root=original,
            grid_identity=_GID,
            context=_CTX,
            created_utc="20260611T110000Z",
        )
        self._execute(original, plan)
        # Re-sweep the healed tree: the condemned tuple fully redoes.
        with AuditWriter.open(original / "audit2.jsonl") as audit:
            run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: _trending_bars(),
                store_for=make_store_for(original, grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                audit=audit,
            )
        control = _store_path(scratch, grid)
        assert store.read_bytes() == control.read_bytes()
        # And the healed tree verifies CLEAN — quarantine excluded (C3).
        assert verify_structural(original).clean
        identity = verify_identity(original, grid)
        assert identity.clean
        assert not any(QUARANTINE_DIRNAME in f.path for f in identity.findings)


# ── C3 — the quarantine glob exclusions (doctor + read-back) ────────────────


class TestQuarantineExclusion:
    def test_results_report_constant_pinned(self) -> None:
        # The pinned-literal cross-check (results_report stays import-lean).
        from brutex.engine.results_report import _QUARANTINE_DIRNAME

        assert _QUARANTINE_DIRNAME == QUARANTINE_DIRNAME

    def test_load_results_tree_skips_quarantine(self, tmp_path: Path) -> None:
        # Healed tree + redone store: without C3 the duplicate run_ids with
        # different metrics would raise the cross-file append conflict.
        from brutex.engine.results_report import load_results_tree

        root, plan = _real_plan(tmp_path)
        assessment = assess_heal(root, plan, sweep_running=False, allow_mass_quarantine=True)
        with AuditWriter.open(_audit(root), resume=True) as audit:
            execute_heal(root, plan, assessment, audit=audit)
        with AuditWriter.open(root / "audit2.jsonl") as audit:
            run_sweep_grid(
                _grid(),
                bars_for=lambda _i, _t: _trending_bars(),
                store_for=make_store_for(root, _grid()),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                audit=audit,
            )
        store = load_results_tree(root)  # must NOT raise on the healed tree
        assert len(store) > 0


class TestCellFindingsNeverReachHeal:
    def test_mixed_grid_and_cell_report_emits_the_grid_plan(self, tmp_path: Path) -> None:
        # DEC-STAGE3-RECOUNT-001 condition B2/B7: cell findings carry a
        # DISTINCT code, so a mixed report builds the GRID heal plan without
        # raising — cells stay structurally unreachable by heal evidence.
        original, _, grid, report = _condemned_tree(tmp_path)
        cell_finding = VerifyFinding(
            path="NIFTY/1minute/BUY_k1_ffffffffffffffff.parquet",
            code="cell_recount_divergence",
            severity="corrupt",
            detail="cell divergence — verdict-grade but NOT heal-grade",
        )
        mixed = TreeVerifyReport(
            root=report.root,
            tier="recount",
            checked_stores=report.checked_stores,
            findings=(*report.findings, cell_finding),
        )
        plan_map = plan_expected_stores(original, grid)  # type: ignore[arg-type]
        plan = build_heal_plan(
            mixed,
            plan_map,
            root=original,
            grid_identity=_GID,
            context=_CTX,
            created_utc="20260611T130000Z",
        )
        assert len(plan.entries) == 1  # only the GRID divergence is heal-grade
