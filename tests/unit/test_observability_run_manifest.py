"""Unit suite for :mod:`brutex.observability.run_manifest` (R28, D-7/D-8/D-17).

Pins the nine EXACT contract keys, the D-8 absent-door law (absent ≠ zero,
never fabricated), the D-7 scalar ``dedup_skips`` + three-key detail, the P4
``per_door_kills``/``actual.funnel`` sub-shapes, the full-contract
``fold_preflight`` fixtures, atomic writes, and the accumulator's
fail-loud-in-memory / fail-soft-at-I/O split.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest
from brutex.observability.run_manifest import (
    DEDUP_KINDS,
    MANIFEST_SCHEMA_VERSION,
    SOURCEABLE_DOORS,
    ManifestAccumulator,
    RunManifest,
    RunManifestError,
    build_funnel,
    fold_preflight,
    load_manifest,
    manifest_path,
    write_manifest,
    write_manifest_guarded,
)
from structlog.testing import capture_logs

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r28_contract_keys_exact_and_ordered": ("R28",),
    "test_r28_manifest_path_law": ("R28",),
    "test_r28_write_load_roundtrip": ("R28",),
    "test_r28_absent_door_law_absent_is_not_zero": ("R28",),
    "test_r28_per_door_kills_ordered_list_sub_shape": ("R28",),
    "test_r28_load_accepts_mapping_door_shape": ("R28",),
    "test_r28_door_shape_violations_refused": ("R28",),
    "test_r28_dedup_skips_scalar_int_plus_detail": ("R28",),
    "test_r28_actual_funnel_sub_shape_validated": ("R28",),
    "test_r28_write_atomic_no_temp_remnant": ("R28",),
    "test_r28_write_rejects_nan": ("R28",),
    "test_r28_write_failure_maps_to_error": ("R28",),
    "test_r28_write_guarded_fail_soft": ("R28",),
    "test_r28_load_error_matrix": ("R28",),
    "test_r28_fold_preflight_full_contract": ("R28",),
    "test_r28_fold_preflight_missing_estimator_honest_none": ("R28",),
    "test_r28_fold_preflight_malformed_estimator_warns_none": ("R28",),
    "test_r28_fold_preflight_malformed_lists_warn_empty": ("R28",),
    "test_r28_build_funnel_ordered_and_validated": ("R28",),
    "test_r28_accumulator_stage_timings_injected_clock": ("R28",),
    "test_r28_accumulator_stage_ended_without_start_warns": ("R28",),
    "test_r28_accumulator_doors_and_dedup_fail_loud": ("R28",),
    "test_r28_accumulator_suspect_latch_and_flush": ("R28",),
    "test_r28_accumulator_set_actual_validates": ("R28",),
    "test_r28_accumulator_flush_fail_soft": ("R28",),
}

_RUN_ID: Final[str] = "cd" * 32

#: The nine FROZEN cross-phase contract keys (contracts file #3).
_CONTRACT_KEYS: Final[tuple[str, ...]] = (
    "run_id",
    "selection",
    "estimate",
    "actual",
    "per_stage_timings",
    "per_door_kills",
    "dedup_skips",
    "tripwires",
    "suspect",
)


def _manifest(**overrides: object) -> RunManifest:
    base: dict[str, object] = {
        "run_id": _RUN_ID,
        "selection": {"symbols": ["NIFTY"]},
        "estimate": None,
        "actual": {"trials": 10},
        "per_stage_timings": {"sweep": 1.5},
        "per_door_kills": {"min_hits": 4},
        "dedup_skips": 0,
        "tripwires": [],
        "suspect": False,
    }
    base.update(overrides)
    return RunManifest(**base)  # type: ignore[arg-type]


class TestContractShape:
    def test_r28_contract_keys_exact_and_ordered(self, tmp_path: Path) -> None:
        """The 9 contract keys lead the payload, byte-order-pinned, additives after."""
        write_manifest(tmp_path, _manifest())
        raw = json.loads(manifest_path(tmp_path, _RUN_ID).read_text(encoding="utf-8"))
        assert tuple(raw.keys())[:9] == _CONTRACT_KEYS
        assert tuple(raw.keys())[9:] == (
            "schema_version",
            "attempt",
            "exit_code",
            "updated_utc",
            "suspect_reasons",
            "dedup_skips_detail",
            "preflight",
            "recorder_degraded",
        )
        assert raw["schema_version"] == MANIFEST_SCHEMA_VERSION == 1
        assert raw["dedup_skips_detail"] == {"cache": 0, "resume": 0, "sentinel": 0}
        assert raw["preflight"] == {"refusals": [], "clamps": []}

    def test_r28_manifest_path_law(self, tmp_path: Path) -> None:
        """Contracts file #3: <results_root>/runs/<run_id>/manifest.json."""
        assert manifest_path(tmp_path, _RUN_ID) == tmp_path / "runs" / _RUN_ID / "manifest.json"

    def test_r28_write_load_roundtrip(self, tmp_path: Path) -> None:
        """A fully-populated manifest survives write→load field-for-field."""
        m = _manifest(
            estimate={"trials": 100, "est_seconds_provisional": 5, "est_bytes": 1024},
            per_door_kills={"min_hits": 4, "cpcv": 1},
            dedup_skips=3,
            tripwires=[{"name": "count_reconcile", "severity": "error"}],
            suspect=True,
            attempt=2,
            exit_code=1,
            updated_utc=99.5,
            suspect_reasons=("count_reconcile",),
            dedup_skips_detail={"cache": 1, "resume": 2, "sentinel": 0},
            preflight={"refusals": [{"symbol": "NIFTY"}], "clamps": []},
            recorder_degraded=True,
        )
        write_manifest(tmp_path, m)
        loaded = load_manifest(tmp_path, _RUN_ID)
        assert loaded == m

    def test_r28_absent_door_law_absent_is_not_zero(self, tmp_path: Path) -> None:
        """D-8 contract fixture: a consumer can distinguish an ABSENT door from an
        OBSERVED zero — absent doors do not appear at all."""
        write_manifest(
            tmp_path, _manifest(per_door_kills={"min_hits": 0})
        )  # observed zero — present
        loaded = load_manifest(tmp_path, _RUN_ID)
        assert loaded.per_door_kills == {"min_hits": 0}
        assert "min_hits" in loaded.per_door_kills  # observed zero IS present
        assert "dsr" not in loaded.per_door_kills  # unobserved door is ABSENT
        assert loaded.per_door_kills.get("dsr") != 0 or "dsr" in loaded.per_door_kills
        raw = json.loads(manifest_path(tmp_path, _RUN_ID).read_text(encoding="utf-8"))
        assert raw["per_door_kills"] == [{"door": "min_hits", "killed": 0}]
        assert all(row["door"] != "dsr" for row in raw["per_door_kills"])

    def test_r28_per_door_kills_ordered_list_sub_shape(self, tmp_path: Path) -> None:
        """P4 sub-shape: per_door_kills serialises as the ORDERED [{door, killed}]
        list in canonical SOURCEABLE_DOORS order regardless of insertion order."""
        write_manifest(
            tmp_path,
            _manifest(per_door_kills={"robustness": 1, "min_hits": 2, "dsr": 3}),
        )
        raw = json.loads(manifest_path(tmp_path, _RUN_ID).read_text(encoding="utf-8"))
        assert raw["per_door_kills"] == [
            {"door": "min_hits", "killed": 2},
            {"door": "dsr", "killed": 3},
            {"door": "robustness", "killed": 1},
        ]

    def test_r28_load_accepts_mapping_door_shape(self, tmp_path: Path) -> None:
        """Tolerance: the original mapping sketch shape also loads."""
        write_manifest(tmp_path, _manifest())
        path = manifest_path(tmp_path, _RUN_ID)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["per_door_kills"] = {"no_bars": 7}
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert load_manifest(tmp_path, _RUN_ID).per_door_kills == {"no_bars": 7}

    def test_r28_door_shape_violations_refused(self, tmp_path: Path) -> None:
        """Unknown doors, negative/bool kills, and junk row shapes all refuse."""
        with pytest.raises(RunManifestError, match="unknown door 'pf_gate'"):
            write_manifest(tmp_path, _manifest(per_door_kills={"pf_gate": 1}))
        with pytest.raises(RunManifestError, match="must be an int >= 0"):
            write_manifest(tmp_path, _manifest(per_door_kills={"dsr": -1}))
        with pytest.raises(RunManifestError, match="must be an int >= 0"):
            write_manifest(tmp_path, _manifest(per_door_kills={"dsr": True}))
        write_manifest(tmp_path, _manifest())
        path = manifest_path(tmp_path, _RUN_ID)
        raw = json.loads(path.read_text(encoding="utf-8"))
        for poison, message in (
            ([{"door": "dsr"}], "rows must be"),
            ("nope", "must be a list or mapping"),
            ([{"door": 5, "killed": 1}], "door must be a str"),
            ([{"door": "dsr", "killed": -2}], "must be an int >= 0"),
        ):
            raw["per_door_kills"] = poison
            path.write_text(json.dumps(raw), encoding="utf-8")
            with pytest.raises(RunManifestError, match=message):
                load_manifest(tmp_path, _RUN_ID)

    def test_r28_dedup_skips_scalar_int_plus_detail(self, tmp_path: Path) -> None:
        """D-7: dedup_skips is a scalar int; detail carries EXACTLY the 3 kinds."""
        assert DEDUP_KINDS == ("cache", "resume", "sentinel")
        write_manifest(
            tmp_path,
            _manifest(dedup_skips=6, dedup_skips_detail={"cache": 3, "resume": 2, "sentinel": 1}),
        )
        raw = json.loads(manifest_path(tmp_path, _RUN_ID).read_text(encoding="utf-8"))
        assert raw["dedup_skips"] == 6
        assert isinstance(raw["dedup_skips"], int)
        assert raw["dedup_skips_detail"] == {"cache": 3, "resume": 2, "sentinel": 1}
        with pytest.raises(RunManifestError, match="exactly"):
            write_manifest(tmp_path, _manifest(dedup_skips_detail={"cache": 1}))
        with pytest.raises(RunManifestError, match="dedup_skips_detail"):
            write_manifest(
                tmp_path,
                _manifest(dedup_skips_detail={"cache": -1, "resume": 0, "sentinel": 0}),
            )
        with pytest.raises(RunManifestError, match="dedup_skips must be an int >= 0"):
            write_manifest(tmp_path, _manifest(dedup_skips=-1))

    def test_r28_actual_funnel_sub_shape_validated(self, tmp_path: Path) -> None:
        """P4 sub-shape: actual.funnel = ordered [{stage, count}] +
        enumerated/tested/skipped ints — validated, never fabricated."""
        m = _manifest(
            actual={
                "funnel": [{"stage": "enumerated", "count": 5}, {"stage": "tested", "count": 4}],
                "enumerated": 5,
                "tested": 4,
                "skipped": 1,
            }
        )
        write_manifest(tmp_path, m)
        raw = json.loads(manifest_path(tmp_path, _RUN_ID).read_text(encoding="utf-8"))
        assert raw["actual"]["funnel"] == [
            {"stage": "enumerated", "count": 5},
            {"stage": "tested", "count": 4},
        ]
        assert raw["actual"]["enumerated"] == 5
        # An actual WITHOUT the sub-shapes stays as-given (nothing fabricated).
        write_manifest(tmp_path, _manifest(actual={"trials": 1}))
        raw = json.loads(manifest_path(tmp_path, _RUN_ID).read_text(encoding="utf-8"))
        assert raw["actual"] == {"trials": 1}
        with pytest.raises(RunManifestError, match="actual.funnel must be an ordered list"):
            write_manifest(tmp_path, _manifest(actual={"funnel": {"stage": "x"}}))
        with pytest.raises(RunManifestError, match="funnel rows"):
            write_manifest(tmp_path, _manifest(actual={"funnel": [{"stage": "x"}]}))
        with pytest.raises(RunManifestError, match="actual.tested must be an int"):
            write_manifest(tmp_path, _manifest(actual={"tested": "many"}))

    def test_r28_write_atomic_no_temp_remnant(self, tmp_path: Path) -> None:
        """The atomic write leaves exactly the manifest — no .brutex-write-* temp."""
        write_manifest(tmp_path, _manifest())
        write_manifest(tmp_path, _manifest(dedup_skips=1))  # rewrite over existing
        run_dir = manifest_path(tmp_path, _RUN_ID).parent
        assert [p.name for p in run_dir.iterdir()] == ["manifest.json"]
        assert load_manifest(tmp_path, _RUN_ID).dedup_skips == 1

    def test_r28_write_rejects_nan(self, tmp_path: Path) -> None:
        """RFC 8259 strictness: a NaN float refuses loudly, never lands on disk."""
        with pytest.raises(RunManifestError, match="not JSON-serialisable"):
            write_manifest(tmp_path, _manifest(actual={"pnl": float("nan")}))
        assert not manifest_path(tmp_path, _RUN_ID).exists()

    def test_r28_write_failure_maps_to_error(self, tmp_path: Path) -> None:
        """An OSError during the atomic write maps to RunManifestError."""
        blocker = tmp_path / "runs"
        blocker.write_text("not a directory", encoding="utf-8")
        with pytest.raises(RunManifestError, match="failed to write manifest"):
            write_manifest(tmp_path, _manifest())

    def test_r28_write_guarded_fail_soft(self, tmp_path: Path) -> None:
        """The guarded twin returns the path on success; WARN + None on failure."""
        assert write_manifest_guarded(tmp_path, _manifest()) == manifest_path(tmp_path, _RUN_ID)
        bad = _manifest(per_door_kills={"not_a_door": 1})
        with capture_logs() as logs:
            assert write_manifest_guarded(tmp_path, bad) is None
        warns = [e for e in logs if e["event"] == "run_manifest.write_failed"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "RunManifestError"

    def test_r28_load_error_matrix(self, tmp_path: Path) -> None:
        """Missing file / junk JSON / non-object root / missing contract keys /
        wrong field types all raise RunManifestError naming the defect."""
        with pytest.raises(RunManifestError, match="cannot read manifest"):
            load_manifest(tmp_path, _RUN_ID)
        path = manifest_path(tmp_path, _RUN_ID)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(RunManifestError, match="malformed manifest JSON"):
            load_manifest(tmp_path, _RUN_ID)
        path.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(RunManifestError, match="root must be an object"):
            load_manifest(tmp_path, _RUN_ID)
        path.write_text(json.dumps({"run_id": _RUN_ID}), encoding="utf-8")
        with pytest.raises(RunManifestError, match="missing contract keys"):
            load_manifest(tmp_path, _RUN_ID)
        write_manifest(tmp_path, _manifest())
        good = json.loads(path.read_text(encoding="utf-8"))
        for key, poison, message in (
            ("run_id", 7, "run_id must be a str"),
            ("selection", 3, "selection must be a mapping"),
            ("estimate", 5, "estimate must be a mapping or null"),
            ("actual", "x", "actual must be a mapping"),
            ("per_stage_timings", "x", "per_stage_timings must be a mapping"),
            ("per_stage_timings", {"sweep": "fast"}, "must be a number"),
            ("dedup_skips", 1.5, "dedup_skips must be an int >= 0"),
            ("tripwires", "none", "tripwires must be a list"),
            ("tripwires", ["oops"], "rows must be mappings"),
            ("suspect", "yes", "suspect must be a bool"),
            ("exit_code", "zero", "exit_code must be an int or null"),
            ("suspect_reasons", [1], "suspect_reasons must be a list of str"),
            ("preflight", 9, "preflight must be a mapping"),
            ("dedup_skips_detail", {"cache": 1}, "exactly"),
            ("dedup_skips_detail", {"cache": "x", "resume": 0, "sentinel": 0}, "int >= 0"),
        ):
            poisoned = dict(good)
            poisoned[key] = poison
            path.write_text(json.dumps(poisoned), encoding="utf-8")
            with pytest.raises(RunManifestError, match=message):
                load_manifest(tmp_path, _RUN_ID)


class TestFoldPreflight:
    def test_r28_fold_preflight_full_contract(self) -> None:
        """D-17 contract fixture: refusals + clamps + estimator all fold."""
        verdict = {
            "ok": False,
            "refusals": [
                {"symbol": "NIFTY", "tf": "5minute", "missing_spans": ["2020-01"], "reason": "gap"}
            ],
            "clamps": [{"field": "start", "clamped_to": "2020-01-01"}],
            "estimator": {"trials": 1770, "est_seconds_provisional": 60, "est_bytes": 4096},
            "symbols": ["NIFTY"],
            "k": 2,
        }
        selection, estimate, preflight = fold_preflight(verdict)
        assert selection == {"symbols": ["NIFTY"], "k": 2}
        assert estimate == {"trials": 1770, "est_seconds_provisional": 60, "est_bytes": 4096}
        assert preflight == {
            "refusals": [
                {"symbol": "NIFTY", "tf": "5minute", "missing_spans": ["2020-01"], "reason": "gap"}
            ],
            "clamps": [{"field": "start", "clamped_to": "2020-01-01"}],
        }

    def test_r28_fold_preflight_missing_estimator_honest_none(self) -> None:
        """No estimator (absent or null) → honest None estimate, never fabricated."""
        for verdict in ({"ok": True}, {"ok": True, "estimator": None}):
            selection, estimate, preflight = fold_preflight(verdict)
            assert estimate is None
            assert selection == {}
            assert preflight == {"refusals": [], "clamps": []}

    def test_r28_fold_preflight_malformed_estimator_warns_none(self) -> None:
        """A malformed estimator → ONE WARN + honest None (Golden Rule #1)."""
        for junk in (
            "soon",
            {"trials": "many"},
            {"trials": 1},
            {"trials": True, "est_seconds_provisional": 1, "est_bytes": 1},
        ):
            with capture_logs() as logs:
                _selection, estimate, _preflight = fold_preflight({"ok": True, "estimator": junk})
            assert estimate is None
            warns = [e for e in logs if e["event"] == "run_manifest.estimator_malformed"]
            assert len(warns) == 1

    def test_r28_fold_preflight_malformed_lists_warn_empty(self) -> None:
        """Non-list refusals/clamps degrade to [] with a WARN — never a crash."""
        with capture_logs() as logs:
            _sel, _est, preflight = fold_preflight({"refusals": "nope", "clamps": ("a",)})
        assert preflight == {"refusals": [], "clamps": ["a"]}  # tuple tolerated, str refused
        warns = [e for e in logs if e["event"] == "run_manifest.preflight_field_malformed"]
        assert len(warns) == 1
        assert warns[0]["key"] == "refusals"


class TestBuildFunnel:
    def test_r28_build_funnel_ordered_and_validated(self) -> None:
        """build_funnel preserves insertion order; junk fails loud."""
        rows = build_funnel({"enumerated": 10, "tested": 8, "kept": 2})
        assert rows == [
            {"stage": "enumerated", "count": 10},
            {"stage": "tested", "count": 8},
            {"stage": "kept", "count": 2},
        ]
        assert build_funnel({}) == []
        with pytest.raises(ValueError, match="stage must be a str"):
            build_funnel({1: 2})  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="must be an int >= 0"):
            build_funnel({"tested": -1})
        with pytest.raises(ValueError, match="must be an int >= 0"):
            build_funnel({"tested": True})


class TestAccumulator:
    def _acc(self, tmp_path: Path, ticks: list[float]) -> ManifestAccumulator:
        clock_values = iter(ticks)
        return ManifestAccumulator(
            results_root=tmp_path,
            run_id=_RUN_ID,
            selection={"symbols": ["NIFTY"]},
            estimate={"trials": 5, "est_seconds_provisional": 1, "est_bytes": 10},
            attempt=2,
            preflight={"refusals": [], "clamps": ["c"]},
            monotonic=lambda: next(clock_values),
            wall=lambda: 777.0,
        )

    def test_r28_accumulator_stage_timings_injected_clock(self, tmp_path: Path) -> None:
        """stage_started/stage_ended derive EXACT durations; re-entry accumulates."""
        acc = self._acc(tmp_path, [10.0, 12.5, 20.0, 21.25])
        acc.stage_started("sweep")
        acc.stage_ended("sweep")  # 2.5 s
        acc.stage_started("sweep")
        acc.stage_ended("sweep")  # +1.25 s
        snap = acc.snapshot()
        assert snap.per_stage_timings == {"sweep": 3.75}
        assert snap.attempt == 2
        assert snap.updated_utc == 777.0
        assert snap.estimate == {"trials": 5, "est_seconds_provisional": 1, "est_bytes": 10}
        assert snap.preflight == {"refusals": [], "clamps": ["c"]}

    def test_r28_accumulator_stage_ended_without_start_warns(self, tmp_path: Path) -> None:
        """stage_ended with no start is a WARNed no-op — never a fabricated timing."""
        acc = self._acc(tmp_path, [])
        with capture_logs() as logs:
            acc.stage_ended("cascade")
        warns = [e for e in logs if e["event"] == "run_manifest.stage_not_started"]
        assert len(warns) == 1
        assert warns[0]["stage"] == "cascade"
        assert acc.snapshot().per_stage_timings == {}

    def test_r28_accumulator_doors_and_dedup_fail_loud(self, tmp_path: Path) -> None:
        """In-memory mutation validates fail-loud; totals derive from detail."""
        acc = self._acc(tmp_path, [])
        acc.add_door_kills("min_hits", 3)
        acc.add_door_kills("min_hits", 2)
        acc.add_door_kills("cpcv", 0)  # an OBSERVED zero is a present door (D-8)
        acc.add_dedup_skip("cache")
        acc.add_dedup_skip("resume", 4)
        with pytest.raises(ValueError, match="unknown per_door_kills door 'pf'"):
            acc.add_door_kills("pf", 1)
        with pytest.raises(ValueError, match="int >= 0"):
            acc.add_door_kills("dsr", -1)
        with pytest.raises(ValueError, match="unknown dedup-skip kind 'bloom'"):
            acc.add_dedup_skip("bloom")
        with pytest.raises(ValueError, match="int >= 0"):
            acc.add_dedup_skip("cache", -2)
        snap = acc.snapshot()
        assert snap.per_door_kills == {"min_hits": 5, "cpcv": 0}
        assert snap.dedup_skips == 5  # scalar == sum of detail (D-7)
        assert snap.dedup_skips_detail == {"cache": 1, "resume": 4, "sentinel": 0}
        assert SOURCEABLE_DOORS == (
            "min_hits",
            "no_bars",
            "cache_skip",
            "resume_skip",
            "dsr",
            "spa",
            "cpcv",
            "walk_forward",
            "k_of_n",
            "robustness",
        )

    def test_r28_accumulator_suspect_latch_and_flush(self, tmp_path: Path) -> None:
        """mark_suspect latches + dedupes reasons; flush persists atomically."""
        acc = self._acc(tmp_path, [])
        acc.add_tripwire({"name": "nan_screen", "severity": "error"})
        acc.mark_suspect("nan_screen")
        acc.mark_suspect("nan_screen")  # dedup
        acc.mark_suspect("count_reconcile")
        acc.set_exit_code(3)
        acc.set_recorder_degraded(True)
        path = acc.flush()
        assert path == manifest_path(tmp_path, _RUN_ID)
        loaded = load_manifest(tmp_path, _RUN_ID)
        assert loaded.suspect is True
        assert loaded.suspect_reasons == ("nan_screen", "count_reconcile")
        assert loaded.tripwires == [{"name": "nan_screen", "severity": "error"}]
        assert loaded.exit_code == 3
        assert loaded.recorder_degraded is True

    def test_r28_accumulator_set_actual_validates(self, tmp_path: Path) -> None:
        """set_actual applies the same P4 sub-shape validation as the writer."""
        acc = self._acc(tmp_path, [])
        acc.set_actual({"funnel": build_funnel({"tested": 4}), "tested": 4})
        assert acc.snapshot().actual == {"funnel": [{"stage": "tested", "count": 4}], "tested": 4}
        with pytest.raises(RunManifestError, match="actual.funnel"):
            acc.set_actual({"funnel": "junk"})

    def test_r28_accumulator_flush_fail_soft(self, tmp_path: Path) -> None:
        """A flush hitting an unwritable root WARNs + returns None (I-3)."""
        acc = self._acc(tmp_path, [])
        (tmp_path / "runs").write_text("blocker", encoding="utf-8")
        with capture_logs() as logs:
            assert acc.flush() is None
        assert [e for e in logs if e["event"] == "run_manifest.write_failed"]
