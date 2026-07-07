"""Unit driver for :mod:`brutex.observability.bench_baseline` (100% line+branch).

DEC-BENCH-ABS-BASELINE-001. Every §3.3 failure-permutation row of the final
spec is a dedicated test here (each doubles as the STANDARDS-row-10
regression pin); the compare boundary law (threshold equality PASSES, one
ulp above FAILS — the <100 tolerance cap PLUS the float-exact
``1 + t/100 < 2.0`` multiplier law carry the 2x guarantee), the capture
refusals, and the full governance permutation grid are all pinned.
"""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from brutex.observability import bench_baseline as bb

if TYPE_CHECKING:
    from collections.abc import Mapping

# ────────────────────────────────────────────────────────────────────────
# Shared factories (synthetic, clearly-labelled test data)
# ────────────────────────────────────────────────────────────────────────

REPO_SLUG = "SJParthi/bruteX"
FP: dict[str, int | str] = {
    "model_slug": "testbox",
    "arch": "x86-64",
    "os_family": "linux",
    "physical_cores": 4,
    "performance_cores": 2,
    "efficiency_cores": 2,
    "total_ram_gib": 8,
}
KEY = "testbox-4c2p2e-8g-x86-64"
FILENAME = f"{KEY}.json"
RUN_URL = "https://github.com/SJParthi/bruteX/actions/runs/1234567"
RUN_URL_2 = "https://github.com/SJParthi/bruteX/actions/runs/7654321"
COMMIT = "0" * 40
COMMIT_2 = "1" * 40
CONTEXT = {"python": "3.12.8", "numpy": "2.2.1", "os_version": "15.5"}
NEW_DEC = "DEC-BENCH-REBLESS-TEST-002"


def make_provenance(**over: str) -> dict[str, str]:
    """One valid provenance object."""
    doc = {
        "run_url": RUN_URL,
        "commit": COMMIT,
        "captured_utc": "2026-07-06T00:00:00Z",
        "runner_name": "test-runner",
    }
    doc.update(over)
    return doc


def make_entry_doc(entry: str = "bar_lookup", **over: Any) -> dict[str, Any]:
    """One valid baseline entry cell (synthetic 100 ns/op)."""
    doc: dict[str, Any] = {
        "unit": bb.UNIT,
        "stat": bb.STAT,
        "value_ns_per_op": 100.0,
        "samples_ns_per_op": [100.0, 110.0, 105.0],
        "spread_pct": 3.0,
        "tolerance_pct": 50.0,
        "improvement_warn_ratio": 2.0,
        "suspicious_speedup_ratio": 4.0,
        "workload_hash": bb.workload_hash(entry),
        "decision_id": bb.BOOTSTRAP_DECISION_ID,
        "provenance": make_provenance(),
        "note": "SYNTHETIC test cell",
    }
    doc.update(over)
    return doc


def make_baseline_doc(entries: Mapping[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    """One valid baseline document over the synthetic test hardware key."""
    if entries is None:
        entries = {entry: make_entry_doc(entry) for entry in sorted(bb.REQUIRED_ENTRY_KEYS)}
    doc: dict[str, Any] = {
        "schema_version": bb.SCHEMA_VERSION,
        "hardware_key": KEY,
        "fingerprint": dict(FP),
        "context": dict(CONTEXT),
        "entries": dict(entries),
    }
    doc.update(over)
    return doc


def parse_doc(doc: Mapping[str, Any], filename: str = FILENAME) -> bb.Baseline:
    """json.dumps + parse_baseline in one step."""
    return bb.parse_baseline(json.dumps(doc), filename=filename)


def make_fragment(entry: str, value: float = 100.0, **over: Any) -> bb.Fragment:
    """One valid measured fragment for *entry*."""
    doc: dict[str, Any] = {
        "entry": entry,
        "unit": bb.UNIT,
        "stat": bb.STAT,
        "value_ns_per_op": value,
        "samples_ns_per_op": [value, value * 1.1, value * 1.05],
        "spread_pct": 2.0,
        "ops_per_round": 1000,
        "rounds": 3,
        "workload_hash": bb.workload_hash(entry),
        "wheels": {"fno_math": True, "trading_core": True},
        "context": dict(CONTEXT),
    }
    doc.update(over)
    return bb.parse_fragment(json.dumps(doc), entry_hint=entry)


def all_fragments(value: float = 100.0) -> dict[str, bb.Fragment]:
    """A full registry-shaped fragment set at one measured value."""
    return {entry: make_fragment(entry, value) for entry in sorted(bb.REQUIRED_ENTRY_KEYS)}


def run_ok(_url: str) -> Mapping[str, object]:
    """A run verifier that confirms any URL as a successful bench run."""
    return {
        "path": bb.BENCH_WORKFLOW_PATH,
        "status": "completed",
        "conclusion": "success",
        "head_sha": COMMIT,
    }


def make_run_verifier(**fields: object) -> Any:
    """A run verifier returning a doctored run document."""
    doc: dict[str, object] = {
        "path": bb.BENCH_WORKFLOW_PATH,
        "status": "completed",
        "conclusion": "success",
        "head_sha": COMMIT,
    }
    doc.update(fields)

    def verify(_url: str) -> Mapping[str, object]:
        return doc

    return verify


# ────────────────────────────────────────────────────────────────────────
# Slug / key / hash primitives
# ────────────────────────────────────────────────────────────────────────


class TestSlugAndKey:
    def test_slugify_lowercases_and_collapses(self) -> None:
        assert bb.slugify("Mac16,8") == "mac16-8"
        assert bb.slugify("Intel(R) Xeon(R) @ 2.10GHz") == "intel-r-xeon-r-2-10ghz"

    def test_slugify_empty_degrades_to_unknown(self) -> None:
        assert bb.slugify("***") == "unknown"
        assert bb.slugify("") == "unknown"

    def test_format_hardware_key_reference_box(self) -> None:
        key = bb.format_hardware_key(
            model_slug="mac16-8",
            physical_cores=14,
            performance_cores=10,
            efficiency_cores=4,
            total_ram_gib=48,
            arch="arm64",
        )
        assert key == "mac16-8-14c10p4e-48g-arm64"
        assert bb.KEY_SLUG_RE.fullmatch(key)

    def test_format_hardware_key_unknown_ram_renders_unk(self) -> None:
        key = bb.format_hardware_key(
            model_slug="box",
            physical_cores=2,
            performance_cores=2,
            efficiency_cores=0,
            total_ram_gib=0,
            arch="x86-64",
        )
        assert "-unkg-" in key

    def test_workload_hash_shape_and_determinism(self) -> None:
        first = bb.workload_hash("bar_lookup")
        assert bb.WORKLOAD_HASH_RE.fullmatch(first)
        assert first == bb.workload_hash("bar_lookup")

    def test_workload_hash_unknown_entry_raises(self) -> None:
        with pytest.raises(bb.BenchBaselineError, match="unknown entry id"):
            bb.workload_hash("nonexistent_hot_path")

    def test_core_is_a_pure_stdlib_leaf_with_zero_brutex_imports(self) -> None:
        """CodeQL py/cyclic-import 670/671 regression pin: the core must
        carry ZERO brutex imports — module scope AND function scope.

        ``hardware_envelope`` imports the ONE key law FROM this module, so
        ANY brutex import here (even the old lazy ``runtime_hardware_key``
        one) re-creates the config <-> observability cycle. The runtime-key
        lookup now lives in ``scripts/ci/bench_baseline_gate.py::cmd_key``.
        """
        source = Path(bb.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=bb.__file__)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"line {node.lineno}: import {alias.name}"
                    for alias in node.names
                    if alias.name.split(".")[0] == "brutex"
                )
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "brutex":
                offenders.append(f"line {node.lineno}: from {node.module} import ...")
        assert offenders == []

    def test_tolerance_cap_is_strictly_below_two_x(self) -> None:
        """The structural 2x-must-fail invariant (S6) — FLOAT-EXACT.

        The raw ``[0, 100)`` range check alone is NOT sufficient: the largest
        legal double below 100 has a failure multiplier that ROUNDS to
        exactly 2.0 in IEEE double, so the schema additionally requires
        ``1 + tolerance/100 < 2.0`` float-exactly (review round 2)."""
        assert bb.TOLERANCE_PCT_MAX_EXCLUSIVE == 100.0
        assert bb.TOLERANCE_MULTIPLIER_MAX_EXCLUSIVE == 2.0
        boundary = math.nextafter(100.0, 0.0)
        assert boundary < bb.TOLERANCE_PCT_MAX_EXCLUSIVE  # passes the raw range…
        assert 1.0 + boundary / 100.0 == 2.0  # …but the multiplier hits 2.0 exactly
        # The helper IS that raw arithmetic (the ONE-source-of-truth pin).
        assert bb.tolerance_multiplier(boundary) == 2.0
        assert bb.tolerance_multiplier(50.0) == 1.5


# ────────────────────────────────────────────────────────────────────────
# parse_baseline — every schema-law permutation
# ────────────────────────────────────────────────────────────────────────


class TestParseBaseline:
    def test_happy_path(self) -> None:
        baseline = parse_doc(make_baseline_doc())
        assert baseline.hardware_key == KEY
        assert set(baseline.entries) == bb.REQUIRED_ENTRY_KEYS
        assert baseline.entries["bar_lookup"].value_ns_per_op == 100.0
        assert baseline.entries["bar_lookup"].note == "SYNTHETIC test cell"

    def test_partial_baseline_is_legal(self) -> None:
        doc = make_baseline_doc(entries={"bar_lookup": make_entry_doc("bar_lookup")})
        assert set(parse_doc(doc).entries) == {"bar_lookup"}

    def test_corrupt_json(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="invalid JSON at offset"):
            bb.parse_baseline("{not json", filename=FILENAME)

    def test_empty_file(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="invalid JSON"):
            bb.parse_baseline("", filename=FILENAME)

    def test_non_dict_root(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="root must be an object"):
            bb.parse_baseline("[1, 2]", filename=FILENAME)

    def test_bom_prefixed_file(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="invalid JSON"):
            bb.parse_baseline("﻿" + json.dumps(make_baseline_doc()), filename=FILENAME)

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_literals_rejected(self, literal: str) -> None:
        text = json.dumps(make_baseline_doc()).replace("100.0", literal, 1)
        with pytest.raises(bb.BaselineSchemaError, match="non-finite JSON literal"):
            bb.parse_baseline(text, filename=FILENAME)

    def test_missing_and_unknown_top_level_keys(self) -> None:
        doc = make_baseline_doc()
        del doc["context"]
        doc["surprise"] = 1
        with pytest.raises(bb.BaselineSchemaError, match=r"missing keys \['context'\]"):
            parse_doc(doc)

    def test_wrong_schema_version(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="schema_version 2 != supported 1"):
            parse_doc(make_baseline_doc(schema_version=2))

    def test_schema_version_not_int(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="must be an integer"):
            parse_doc(make_baseline_doc(schema_version="1"))

    def test_hardware_key_not_string(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="hardware_key must be a string"):
            parse_doc(make_baseline_doc(hardware_key=5))

    def test_hardware_key_fails_slug_law(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="fails the slug law"):
            parse_doc(make_baseline_doc(hardware_key="Bad Key!"), filename="Bad Key!.json")

    def test_filename_stem_mismatch(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="filename stem"):
            parse_doc(make_baseline_doc(), filename="other-key.json")

    def test_filename_without_json_suffix_uses_full_name(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="filename stem"):
            parse_doc(make_baseline_doc(), filename=KEY + ".txt")

    def test_fingerprint_not_object(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="fingerprint must be an object"):
            parse_doc(make_baseline_doc(fingerprint=[1]))

    def test_fingerprint_missing_field(self) -> None:
        fp = dict(FP)
        del fp["arch"]
        with pytest.raises(bb.BaselineSchemaError, match=r"missing keys \['arch'\]"):
            parse_doc(make_baseline_doc(fingerprint=fp))

    def test_fingerprint_wrong_type(self) -> None:
        fp = dict(FP)
        fp["physical_cores"] = "4"
        with pytest.raises(bb.BaselineSchemaError, match="must be an integer"):
            parse_doc(make_baseline_doc(fingerprint=fp))

    def test_fingerprint_bool_rejected(self) -> None:
        fp = dict(FP)
        fp["physical_cores"] = True
        with pytest.raises(bb.BaselineSchemaError, match="must be an integer"):
            parse_doc(make_baseline_doc(fingerprint=fp))

    def test_fingerprint_negative_int(self) -> None:
        fp = dict(FP)
        fp["total_ram_gib"] = -1
        with pytest.raises(bb.BaselineSchemaError, match="must be >= 0"):
            parse_doc(make_baseline_doc(fingerprint=fp))

    def test_key_does_not_rederive_from_fingerprint(self) -> None:
        fp = dict(FP)
        fp["total_ram_gib"] = 16  # hand-edited fingerprint, key untouched
        with pytest.raises(bb.BaselineSchemaError, match="does not re-derive"):
            parse_doc(make_baseline_doc(fingerprint=fp))

    def test_context_not_str_mapping(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="context must be an object"):
            parse_doc(make_baseline_doc(context=[1]))

    def test_context_non_str_value(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="must be a string"):
            parse_doc(make_baseline_doc(context={"python": 3.12}))

    def test_context_missing_python(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="context.python is required"):
            parse_doc(make_baseline_doc(context={"numpy": "2.2.1"}))

    def test_entries_not_object(self) -> None:
        doc = make_baseline_doc()
        doc["entries"] = []
        with pytest.raises(bb.BaselineSchemaError, match="entries must be an object"):
            parse_doc(doc)

    def test_orphan_entry_rejected(self) -> None:
        doc = make_baseline_doc(entries={"rogue_path": make_entry_doc("bar_lookup")})
        with pytest.raises(bb.BaselineSchemaError, match=r"orphan entries \['rogue_path'\]"):
            parse_doc(doc)


class TestParseEntryCell:
    def _bad_entry(self, **over: Any) -> dict[str, Any]:
        return make_baseline_doc(entries={"bar_lookup": make_entry_doc("bar_lookup", **over)})

    def test_entry_not_object(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="entry must be an object"):
            parse_doc(make_baseline_doc(entries={"bar_lookup": 5}))

    def test_entry_unknown_key(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match=r"unknown keys \['surprise'\]"):
            parse_doc(self._bad_entry(surprise=1))

    def test_unit_mismatch(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="unit must be 'ns_per_op'"):
            parse_doc(self._bad_entry(unit="ops_per_sec"))

    def test_stat_mismatch(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="stat must be 'min'"):
            parse_doc(self._bad_entry(stat="median"))

    @pytest.mark.parametrize("value", [0, -5.0, "100"])
    def test_bad_value(self, value: Any) -> None:
        with pytest.raises(bb.BaselineSchemaError):
            parse_doc(self._bad_entry(value_ns_per_op=value))

    def test_value_not_min_of_samples(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="!= min"):
            parse_doc(self._bad_entry(value_ns_per_op=99.0))

    def test_samples_not_list(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="must be a nonempty list"):
            parse_doc(self._bad_entry(samples_ns_per_op="nope"))

    def test_samples_empty(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="must be a nonempty list"):
            parse_doc(self._bad_entry(samples_ns_per_op=[]))

    def test_samples_too_few(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="needs >= 3 samples"):
            parse_doc(self._bad_entry(samples_ns_per_op=[100.0, 101.0]))

    def test_sample_non_numeric(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match=r"samples_ns_per_op\[1\]"):
            parse_doc(self._bad_entry(samples_ns_per_op=[100.0, "x", 105.0]))

    def test_sample_bool_rejected(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match=r"samples_ns_per_op\[0\]"):
            parse_doc(self._bad_entry(samples_ns_per_op=[True, 100.0, 105.0]))

    def test_sample_zero_rejected(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="finite and > 0"):
            parse_doc(self._bad_entry(samples_ns_per_op=[0, 100.0, 105.0]))

    def test_spread_negative(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="spread_pct must be >= 0"):
            parse_doc(self._bad_entry(spread_pct=-0.1))

    @pytest.mark.parametrize("tolerance", [-0.1, 100.0, 250.0, math.nextafter(100.0, 0.0)])
    def test_tolerance_out_of_cap(self, tolerance: float) -> None:
        """The strict [0, 100) cap — 100.0 itself is REJECTED (S6), and so is
        the max legal double BELOW 100 (its multiplier 1 + t/100 rounds to
        exactly 2.0, which would let an exact-2x slowdown pass — the
        float-exact multiplier law, review round 2)."""
        with pytest.raises(bb.BaselineSchemaError, match="tolerance_pct must be in"):
            parse_doc(self._bad_entry(tolerance_pct=tolerance))

    def test_tolerance_below_the_float_boundary_is_legal(self) -> None:
        """The largest tolerance whose multiplier stays float-exactly below
        2.0 parses — the rejection above is the boundary, not a wider cut."""
        legal = math.nextafter(math.nextafter(100.0, 0.0), 0.0)
        assert 1.0 + legal / 100.0 < 2.0
        baseline = parse_doc(self._bad_entry(tolerance_pct=legal))
        assert baseline.entries["bar_lookup"].tolerance_pct == legal

    def test_tolerance_zero_is_legal(self) -> None:
        baseline = parse_doc(self._bad_entry(tolerance_pct=0.0))
        assert baseline.entries["bar_lookup"].tolerance_pct == 0.0

    def test_improvement_warn_ratio_below_one(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="improvement_warn_ratio"):
            parse_doc(self._bad_entry(improvement_warn_ratio=0.5))

    def test_suspicious_not_above_warn(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="suspicious_speedup_ratio"):
            parse_doc(self._bad_entry(suspicious_speedup_ratio=2.0))

    def test_workload_hash_malformed(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="workload_hash"):
            parse_doc(self._bad_entry(workload_hash="sha256:zz"))

    def test_decision_id_malformed(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="decision_id"):
            parse_doc(self._bad_entry(decision_id="dec-lowercase-001"))

    def test_note_non_string(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="note must be a string"):
            parse_doc(self._bad_entry(note=5))

    def test_note_optional(self) -> None:
        entry = make_entry_doc("bar_lookup")
        del entry["note"]
        baseline = parse_doc(make_baseline_doc(entries={"bar_lookup": entry}))
        assert baseline.entries["bar_lookup"].note == ""

    def test_provenance_not_object(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="provenance must be an object"):
            parse_doc(self._bad_entry(provenance="x"))

    def test_provenance_missing_key(self) -> None:
        prov = make_provenance()
        del prov["commit"]
        with pytest.raises(bb.BaselineSchemaError, match=r"missing keys \['commit'\]"):
            parse_doc(self._bad_entry(provenance=prov))

    def test_run_url_wrong_shape(self) -> None:
        prov = make_provenance(run_url="https://evil.example/actions/runs/1")
        with pytest.raises(bb.BaselineSchemaError, match="run_url"):
            parse_doc(self._bad_entry(provenance=prov))

    def test_commit_not_40_hex(self) -> None:
        prov = make_provenance(commit="abc123")
        with pytest.raises(bb.BaselineSchemaError, match="40-hex"):
            parse_doc(self._bad_entry(provenance=prov))

    def test_captured_utc_garbage(self) -> None:
        prov = make_provenance(captured_utc="yesterday")
        with pytest.raises(bb.BaselineSchemaError, match="not ISO-8601"):
            parse_doc(self._bad_entry(provenance=prov))

    def test_captured_utc_naive(self) -> None:
        prov = make_provenance(captured_utc="2026-07-06T00:00:00")
        with pytest.raises(bb.BaselineSchemaError, match="tz-aware UTC"):
            parse_doc(self._bad_entry(provenance=prov))

    def test_captured_utc_non_utc_offset(self) -> None:
        prov = make_provenance(captured_utc="2026-07-06T00:00:00+05:30")
        with pytest.raises(bb.BaselineSchemaError, match="tz-aware UTC"):
            parse_doc(self._bad_entry(provenance=prov))

    def test_runner_name_empty(self) -> None:
        prov = make_provenance(runner_name="")
        with pytest.raises(bb.BaselineSchemaError, match="runner_name"):
            parse_doc(self._bad_entry(provenance=prov))


class TestValidationHelpers:
    """Direct branch drivers for value screens JSON alone cannot reach."""

    def test_finite_float_rejects_inf(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="must be finite"):
            bb._finite_float({"x": math.inf}, "x", where="t")

    def test_finite_float_rejects_nan(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="must be finite"):
            bb._finite_float({"x": math.nan}, "x", where="t")

    def test_finite_float_rejects_bool(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="must be a number"):
            bb._finite_float({"x": True}, "x", where="t")

    def test_samples_field_rejects_non_finite(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="finite and > 0"):
            bb._samples_field({"s": [1.0, math.inf]}, "s", where="t")

    def test_int_field_below_minimum(self) -> None:
        with pytest.raises(bb.BaselineSchemaError, match="must be >= 1"):
            bb._int_field({"n": 0}, "n", where="t", minimum=1)

    def test_major_minor(self) -> None:
        assert bb._major_minor("3.12.8") == "3.12"
        assert bb._major_minor("3") == "3"


# ────────────────────────────────────────────────────────────────────────
# parse_fragment
# ────────────────────────────────────────────────────────────────────────


class TestParseFragment:
    def test_happy_path(self) -> None:
        frag = make_fragment("bar_lookup", 55.0)
        assert frag.entry == "bar_lookup"
        assert frag.value_ns_per_op == 55.0
        assert frag.wheels == {"fno_math": True, "trading_core": True}

    def test_corrupt_json_raises_fragment_error(self) -> None:
        with pytest.raises(bb.FragmentError, match="invalid JSON"):
            bb.parse_fragment("{", entry_hint="bar_lookup")

    def test_entry_hint_mismatch(self) -> None:
        doc = json.loads(
            json.dumps(
                {
                    "entry": "mask_eval_scalar",
                    "unit": bb.UNIT,
                    "stat": bb.STAT,
                    "value_ns_per_op": 100.0,
                    "samples_ns_per_op": [100.0, 110.0],
                    "spread_pct": 2.0,
                    "ops_per_round": 1000,
                    "rounds": 2,
                    "workload_hash": bb.workload_hash("bar_lookup"),
                    "wheels": {"fno_math": True, "trading_core": True},
                    "context": dict(CONTEXT),
                }
            )
        )
        with pytest.raises(bb.FragmentError, match="!= expected"):
            bb.parse_fragment(json.dumps(doc), entry_hint="bar_lookup")

    def test_unknown_key(self) -> None:
        with pytest.raises(bb.FragmentError, match="unknown keys"):
            make_fragment("bar_lookup", surprise=1)

    def test_unit_mismatch(self) -> None:
        with pytest.raises(bb.FragmentError, match="unit must be"):
            make_fragment("bar_lookup", unit="us_per_op")

    def test_stat_mismatch(self) -> None:
        with pytest.raises(bb.FragmentError, match="stat must be"):
            make_fragment("bar_lookup", stat="mean")

    def test_value_not_min(self) -> None:
        with pytest.raises(bb.FragmentError, match="!= min"):
            make_fragment("bar_lookup", samples_ns_per_op=[50.0, 60.0])

    def test_negative_spread(self) -> None:
        with pytest.raises(bb.FragmentError, match="spread_pct"):
            make_fragment("bar_lookup", spread_pct=-1.0)

    def test_zero_ops_per_round(self) -> None:
        with pytest.raises(bb.FragmentError, match="ops_per_round"):
            make_fragment("bar_lookup", ops_per_round=0)

    def test_zero_rounds(self) -> None:
        with pytest.raises(bb.FragmentError, match="rounds"):
            make_fragment("bar_lookup", rounds=0)

    def test_bad_workload_hash(self) -> None:
        with pytest.raises(bb.FragmentError, match="workload_hash"):
            make_fragment("bar_lookup", workload_hash="md5:abc")

    def test_wheels_not_object(self) -> None:
        with pytest.raises(bb.FragmentError, match="wheels must be an object"):
            make_fragment("bar_lookup", wheels=["fno_math"])

    def test_wheels_non_bool(self) -> None:
        with pytest.raises(bb.FragmentError, match="must be a boolean"):
            make_fragment("bar_lookup", wheels={"fno_math": "yes", "trading_core": True})


# ────────────────────────────────────────────────────────────────────────
# compare — the verdict law
# ────────────────────────────────────────────────────────────────────────


def compare_all(
    baseline: bb.Baseline | None, fragments: Mapping[str, bb.Fragment]
) -> bb.CompareReport:
    return bb.compare(baseline, fragments, runtime_key=KEY, runtime_python="3.12.8")


class TestCompare:
    def test_all_ok_with_attestation(self) -> None:
        report = compare_all(parse_doc(make_baseline_doc()), all_fragments(100.0))
        assert report.ok
        assert report.n_compared == len(bb.REQUIRED_ENTRY_KEYS)
        assert not report.failed
        assert not report.abstained_entries
        assert report.attestation_line.startswith("BENCH-ABS GATE RAN: compared 6/6 entries")
        assert f"tests/bench/baselines/{KEY}.json" in report.attestation_line
        assert "2026-07-06" in report.attestation_line
        assert bb.BOOTSTRAP_DECISION_ID in report.attestation_line

    def test_no_baseline_abstains_green_but_loud(self) -> None:
        report = compare_all(None, all_fragments())
        assert report.ok
        assert report.baseline_missing
        assert set(report.abstained_entries) == bb.REQUIRED_ENTRY_KEYS
        assert report.n_compared == 0
        assert report.attestation_line == ""
        assert any("NO BASELINE" in w for w in report.warnings)

    def test_partial_baseline_abstains_only_new_entry(self) -> None:
        doc = make_baseline_doc()
        del doc["entries"]["result_append"]
        report = compare_all(parse_doc(doc), all_fragments())
        assert report.ok
        assert report.abstained_entries == ("result_append",)
        assert report.n_compared == 5
        assert "compared 5/6" in report.attestation_line

    def test_missing_fragment_is_structural(self) -> None:
        fragments = all_fragments()
        del fragments["bar_lookup"]
        report = compare_all(parse_doc(make_baseline_doc()), fragments)
        assert not report.ok
        assert report.has_structural
        assert report.failed == ("fragment-set",)
        assert "missing ['bar_lookup']" in report.verdicts[0].detail

    def test_extra_fragment_is_structural(self) -> None:
        fragments = all_fragments()
        fragments["rogue"] = fragments["bar_lookup"]
        report = compare_all(parse_doc(make_baseline_doc()), fragments)
        assert report.has_structural
        assert "extra ['rogue']" in report.verdicts[0].detail

    def test_foreign_hardware_key_is_structural(self) -> None:
        report = bb.compare(
            parse_doc(make_baseline_doc()),
            all_fragments(),
            runtime_key="other-box-2c2p0e-4g-arm64",
            runtime_python="3.12.8",
        )
        assert report.has_structural
        assert report.failed == ("hardware-key",)

    def test_python_minor_drift_is_structural(self) -> None:
        report = bb.compare(
            parse_doc(make_baseline_doc()),
            all_fragments(),
            runtime_key=KEY,
            runtime_python="3.13.1",
        )
        assert report.has_structural
        assert report.failed == ("context.python",)

    def test_python_patch_drift_is_not_structural(self) -> None:
        report = bb.compare(
            parse_doc(make_baseline_doc()),
            all_fragments(),
            runtime_key=KEY,
            runtime_python="3.12.99",
        )
        assert report.ok

    def test_numpy_drift_warns_but_verdicts_on_numbers(self) -> None:
        fragments = {
            entry: make_fragment(entry, 100.0, context={**CONTEXT, "numpy": "2.3.0"})
            for entry in sorted(bb.REQUIRED_ENTRY_KEYS)
        }
        report = compare_all(parse_doc(make_baseline_doc()), fragments)
        assert report.ok
        assert any("soft context drift on 'numpy'" in w for w in report.warnings)

    def test_missing_soft_context_key_no_warning(self) -> None:
        doc = make_baseline_doc(context={"python": "3.12.8"})
        report = compare_all(parse_doc(doc), all_fragments())
        assert report.ok
        assert not report.warnings

    def test_workload_hash_drift_is_structural(self) -> None:
        fragments = all_fragments()
        fragments["bar_lookup"] = make_fragment("bar_lookup", workload_hash="sha256:" + "ab" * 32)
        report = compare_all(parse_doc(make_baseline_doc()), fragments)
        assert report.has_structural
        assert "bar_lookup" in report.failed
        assert "S11" in next(v.detail for v in report.verdicts if v.entry == "bar_lookup")

    def test_missing_wheel_is_structural(self) -> None:
        fragments = all_fragments()
        fragments["bar_lookup"] = make_fragment(
            "bar_lookup", wheels={"fno_math": False, "trading_core": True}
        )
        report = compare_all(parse_doc(make_baseline_doc()), fragments)
        assert report.has_structural
        assert "fno_math" in next(v.detail for v in report.verdicts if v.entry == "bar_lookup")

    def test_threshold_equality_passes(self) -> None:
        """measured == blessed x 1.5 exactly → PASS (strict >, boundary-pinned)."""
        fragments = all_fragments()
        fragments["bar_lookup"] = make_fragment("bar_lookup", 150.0)
        report = compare_all(parse_doc(make_baseline_doc()), fragments)
        assert report.ok

    def test_one_ulp_above_threshold_fails(self) -> None:
        fragments = all_fragments()
        fragments["bar_lookup"] = make_fragment("bar_lookup", math.nextafter(150.0, math.inf))
        report = compare_all(parse_doc(make_baseline_doc()), fragments)
        assert not report.ok
        assert report.has_numeric_failure
        assert not report.has_structural
        assert report.failed == ("bar_lookup",)

    @pytest.mark.parametrize("factor", [2.0, 10.0])
    def test_constant_factor_slowdown_fails_every_entry(self, factor: float) -> None:
        """THE operator headline: a 2x/10x slowdown fails on EVERY hot path."""
        report = compare_all(parse_doc(make_baseline_doc()), all_fragments(100.0 * factor))
        assert not report.ok
        assert set(report.failed) == bb.REQUIRED_ENTRY_KEYS
        assert all(v.verdict is bb.Verdict.REGRESSION for v in report.verdicts)

    def test_2_5x_faster_warns_stale(self) -> None:
        fragments = all_fragments()
        fragments["bar_lookup"] = make_fragment("bar_lookup", 40.0)
        report = compare_all(parse_doc(make_baseline_doc()), fragments)
        assert report.ok
        verdict = next(v for v in report.verdicts if v.entry == "bar_lookup")
        assert verdict.verdict is bb.Verdict.OK_IMPROVEMENT_WARN
        assert any("baseline stale" in w for w in report.warnings)

    def test_exactly_4x_faster_passes(self) -> None:
        """measured x 4.0 == blessed → NOT suspicious (strict <, boundary)."""
        fragments = all_fragments()
        fragments["bar_lookup"] = make_fragment("bar_lookup", 25.0)
        report = compare_all(parse_doc(make_baseline_doc()), fragments)
        assert report.ok

    def test_beyond_4x_faster_is_suspicious_failure(self) -> None:
        fragments = all_fragments()
        fragments["bar_lookup"] = make_fragment("bar_lookup", math.nextafter(25.0, 0.0))
        report = compare_all(parse_doc(make_baseline_doc()), fragments)
        assert not report.ok
        assert report.has_numeric_failure
        verdict = next(v for v in report.verdicts if v.entry == "bar_lookup")
        assert verdict.verdict is bb.Verdict.SUSPICIOUS_SPEEDUP


class TestRenderReport:
    def test_render_ok_report(self) -> None:
        report = compare_all(parse_doc(make_baseline_doc()), all_fragments())
        text = bb.render_report(report)
        assert "BENCH-ABS GATE RAN" in text
        assert "ops/s" in text.splitlines()[0]
        assert "::error::" not in text

    def test_render_regression_report(self) -> None:
        report = compare_all(parse_doc(make_baseline_doc()), all_fragments(1000.0))
        text = bb.render_report(report)
        assert "::error::bench-abs bar_lookup: REGRESSION" in text
        assert "docs/runbooks/bench-baseline.md" in text
        assert " 10.000 " in text  # the measured/blessed ratio column

    def test_render_abstain_report(self) -> None:
        report = compare_all(None, all_fragments())
        text = bb.render_report(report)
        assert "::warning::bench-abs: NO BASELINE" in text
        assert "::notice::bench-abs: 0 entries compared this run" in text

    def test_render_partial_abstain_names_entries(self) -> None:
        doc = make_baseline_doc()
        del doc["entries"]["result_append"]
        text = bb.render_report(compare_all(parse_doc(doc), all_fragments()))
        assert "abstained entries result_append" in text

    def test_render_warning_lines(self) -> None:
        fragments = all_fragments()
        fragments["bar_lookup"] = make_fragment("bar_lookup", 40.0)
        text = bb.render_report(compare_all(parse_doc(make_baseline_doc()), fragments))
        assert "::warning::bench-abs: bar_lookup" in text


# ────────────────────────────────────────────────────────────────────────
# fold_capture
# ────────────────────────────────────────────────────────────────────────


def capture_runs(
    values: tuple[float, float, float] = (100.0, 101.0, 102.0),
) -> list[dict[str, bb.Fragment]]:
    return [
        {entry: make_fragment(entry, value) for entry in sorted(bb.REQUIRED_ENTRY_KEYS)}
        for value in values
    ]


def fold(
    runs: list[dict[str, bb.Fragment]],
    *,
    existing: bb.Baseline | None = None,
    decision_id: str = bb.BOOTSTRAP_DECISION_ID,
    replace: frozenset[str] = frozenset(),
    fingerprint: dict[str, int | str] | None = None,
    context: dict[str, str] | None = None,
) -> bb.Baseline:
    return bb.fold_capture(
        runs,
        existing=existing,
        decision_id=decision_id,
        replace_entries=replace,
        fingerprint=dict(FP) if fingerprint is None else fingerprint,
        context=dict(CONTEXT) if context is None else context,
        provenance=bb.Provenance(
            run_url=RUN_URL_2,
            commit=COMMIT_2,
            captured_utc="2026-07-07T00:00:00Z",
            runner_name="capture-runner",
        ),
    )


class TestFoldCapture:
    def test_happy_three_run_bootstrap(self) -> None:
        candidate = fold(capture_runs())
        assert candidate.hardware_key == KEY
        cell = candidate.entries["bar_lookup"]
        assert cell.value_ns_per_op == 100.0
        assert len(cell.samples_ns_per_op) == 9  # 3 runs x 3 samples
        assert cell.spread_pct == pytest.approx(2.0)
        assert cell.decision_id == bb.BOOTSTRAP_DECISION_ID
        assert cell.tolerance_pct == bb.DEFAULT_TOLERANCE_PCT
        assert cell.provenance.run_url == RUN_URL_2

    def test_candidate_roundtrips_through_parse(self) -> None:
        candidate = fold(capture_runs())
        parsed = bb.parse_baseline(bb.dump_baseline(candidate), filename=FILENAME)
        assert parsed == candidate

    def test_too_few_runs_refused(self) -> None:
        with pytest.raises(bb.BenchBaselineError, match="requires >= 3"):
            fold(capture_runs()[:2])

    def test_malformed_decision_id_refused(self) -> None:
        with pytest.raises(bb.GovernanceError, match="malformed"):
            fold(capture_runs(), decision_id="not-a-dec-id")

    def test_run_key_set_mismatch_refused(self) -> None:
        runs = capture_runs()
        del runs[1]["bar_lookup"]
        with pytest.raises(bb.FragmentError, match="run 2 measured entries"):
            fold(runs)

    def test_orphan_measured_entry_refused(self) -> None:
        runs = capture_runs()
        for run in runs:
            run["rogue"] = run["bar_lookup"]
        with pytest.raises(bb.FragmentError, match="not in REQUIRED_ENTRY_KEYS"):
            fold(runs)

    def test_cross_run_hash_disagreement_refused(self) -> None:
        runs = capture_runs()
        runs[1]["bar_lookup"] = make_fragment(
            "bar_lookup", 101.0, workload_hash="sha256:" + "cd" * 32
        )
        with pytest.raises(bb.FragmentError, match="cross-run workload hash"):
            fold(runs)

    def test_invalid_sample_refused(self) -> None:
        runs = capture_runs()
        bad = bb.Fragment(
            entry="bar_lookup",
            value_ns_per_op=100.0,
            samples_ns_per_op=(100.0, math.nan),
            spread_pct=0.0,
            ops_per_round=1,
            rounds=2,
            workload_hash=bb.workload_hash("bar_lookup"),
            wheels={"fno_math": True, "trading_core": True},
            context=dict(CONTEXT),
        )
        runs[0]["bar_lookup"] = bad
        with pytest.raises(bb.FragmentError, match="invalid sample"):
            fold(runs)

    def test_noise_refusal_above_guard_band(self) -> None:
        """Cross-run min spread 26% > tolerance/2 (25%) → NoiseRefusal."""
        with pytest.raises(bb.NoiseRefusal, match="quiet the machine"):
            fold(capture_runs((100.0, 110.0, 126.0)))

    def test_noise_boundary_equality_passes(self) -> None:
        """Spread == exactly tolerance/2 passes (strict >)."""
        candidate = fold(capture_runs((100.0, 110.0, 125.0)))
        assert candidate.entries["bar_lookup"].spread_pct == pytest.approx(25.0)

    def test_fully_blessed_capture_carries_every_cell_byte_identical(self) -> None:
        """Review round 1 (MAJOR): a full-registry capture over a fully-blessed
        baseline CARRIES every unlisted cell unchanged — never a refusal, and
        never a re-mint from the fresh (deliberately different) measurement."""
        existing = parse_doc(make_baseline_doc())
        candidate = fold(capture_runs((10.0, 10.1, 10.2)), existing=existing)
        assert candidate.entries == existing.entries
        assert bb.dump_baseline(candidate) == bb.dump_baseline(existing)

    def test_full_registry_additive_bless_on_partial_baseline(self) -> None:
        """Review round 1 (MAJOR) regression: the measurement suite always
        emits ALL REQUIRED_ENTRY_KEYS fragments — folding them against a
        PARTIAL baseline (new hot path #7 / the S11 removed-cell recovery)
        must produce the additive candidate: blessed cells carried
        byte-identical, the abstained entry captured fresh."""
        doc = make_baseline_doc()
        del doc["entries"]["result_append"]
        existing = parse_doc(doc)
        candidate = fold(capture_runs((10.0, 10.1, 10.2)), existing=existing)
        assert set(candidate.entries) == bb.REQUIRED_ENTRY_KEYS
        for entry in bb.REQUIRED_ENTRY_KEYS - {"result_append"}:
            assert candidate.entries[entry] == existing.entries[entry]
            assert candidate.entries[entry].value_ns_per_op == 100.0  # NOT re-minted at 10.0
        assert candidate.entries["result_append"].value_ns_per_op == 10.0
        assert candidate.entries["result_append"].decision_id == bb.BOOTSTRAP_DECISION_ID

    def test_noisy_measurement_of_carried_cell_never_refuses(self) -> None:
        """A carried (blessed, unlisted) cell's fresh measurement is ignored
        entirely — even a wildly noisy one cannot block the additive bless."""
        doc = make_baseline_doc()
        del doc["entries"]["result_append"]
        existing = parse_doc(doc)
        runs = capture_runs((10.0, 10.1, 10.2))
        for run, value in zip(runs, (100.0, 200.0, 400.0), strict=True):
            run["bar_lookup"] = make_fragment("bar_lookup", value)
        candidate = fold(runs, existing=existing)
        assert candidate.entries["bar_lookup"] == existing.entries["bar_lookup"]
        assert candidate.entries["result_append"].value_ns_per_op == 10.0

    def test_replace_with_bootstrap_id_refused(self) -> None:
        existing = parse_doc(make_baseline_doc())
        with pytest.raises(bb.GovernanceError, match="first capture only"):
            fold(capture_runs(), existing=existing, replace=frozenset({"bar_lookup"}))

    def test_replace_without_existing_refused(self) -> None:
        with pytest.raises(bb.OverwriteRefusal, match="no existing baseline"):
            fold(capture_runs(), decision_id=NEW_DEC, replace=frozenset({"bar_lookup"}))

    def test_replace_unknown_entry_refused(self) -> None:
        doc = make_baseline_doc()
        del doc["entries"]["result_append"]
        existing = parse_doc(doc)
        with pytest.raises(bb.OverwriteRefusal, match="not blessed"):
            fold(
                capture_runs(),
                existing=existing,
                decision_id=NEW_DEC,
                replace=frozenset({"result_append"}),
            )

    def test_replace_with_current_id_refused(self) -> None:
        entries = {
            entry: make_entry_doc(entry, decision_id=NEW_DEC)
            for entry in sorted(bb.REQUIRED_ENTRY_KEYS)
        }
        existing = parse_doc(make_baseline_doc(entries=entries))
        with pytest.raises(bb.GovernanceError, match="NEW ledger entry"):
            fold(
                capture_runs(),
                existing=existing,
                decision_id=NEW_DEC,
                replace=frozenset(bb.REQUIRED_ENTRY_KEYS),
            )

    def test_replace_full_happy_path(self) -> None:
        """Replace all cells: fresh id + provenance; tolerance carried; the
        top-level fingerprint/context REFRESHED from the fresh measurement
        (equal values here — the drift case is pinned separately)."""
        existing = parse_doc(make_baseline_doc())
        candidate = fold(
            capture_runs((50.0, 51.0, 52.0)),
            existing=existing,
            decision_id=NEW_DEC,
            replace=frozenset(bb.REQUIRED_ENTRY_KEYS),
        )
        cell = candidate.entries["bar_lookup"]
        assert cell.decision_id == NEW_DEC
        assert cell.value_ns_per_op == 50.0
        assert cell.provenance.run_url == RUN_URL_2
        assert cell.tolerance_pct == existing.entries["bar_lookup"].tolerance_pct
        assert candidate.fingerprint == existing.fingerprint
        assert candidate.context == existing.context

    def test_full_replace_refreshes_context_so_next_compare_is_ok(self) -> None:
        """Review round 1 (MAJOR) pin: the documented python-drift re-bless
        (dispatch capture, NEW decision id, replace ALL entries) must yield a
        baseline whose context is the NEW interpreter's — the very next
        compare is OK, never a permanent 'context.python' STRUCTURAL red."""
        existing = parse_doc(make_baseline_doc())  # blessed on python 3.12.8
        new_context = {"python": "3.13.1", "numpy": "2.3.0", "os_version": "16.0"}
        candidate = fold(
            capture_runs((50.0, 51.0, 52.0)),
            existing=existing,
            decision_id=NEW_DEC,
            replace=frozenset(bb.REQUIRED_ENTRY_KEYS),
            context=dict(new_context),
        )
        assert candidate.context == new_context
        assert candidate.fingerprint == dict(FP)
        fragments = {
            entry: make_fragment(entry, 50.0, context=dict(new_context))
            for entry in sorted(bb.REQUIRED_ENTRY_KEYS)
        }
        report = bb.compare(candidate, fragments, runtime_key=KEY, runtime_python="3.13.1")
        assert report.ok
        assert not report.has_structural
        assert report.n_compared == len(bb.REQUIRED_ENTRY_KEYS)

    def test_partial_replace_keeps_existing_context_honest_red(self) -> None:
        """A PARTIAL re-bless carries the existing context: the carried cells
        were measured under it, so an interpreter drift stays an honest
        STRUCTURAL red until every cell is re-blessed on the new stack."""
        existing = parse_doc(make_baseline_doc())
        candidate = fold(
            capture_runs((50.0, 51.0, 52.0)),
            existing=existing,
            decision_id=NEW_DEC,
            replace=frozenset({"bar_lookup"}),
            context={"python": "3.13.1"},
        )
        assert candidate.context == existing.context
        assert candidate.fingerprint == existing.fingerprint
        assert candidate.entries["bar_lookup"].decision_id == NEW_DEC
        assert candidate.entries["bar_lookup"].value_ns_per_op == 50.0
        for entry in bb.REQUIRED_ENTRY_KEYS - {"bar_lookup"}:
            assert candidate.entries[entry] == existing.entries[entry]

    def test_additive_bless_preserves_untouched_cells(self) -> None:
        """New-entry capture on a partial baseline never touches blessed cells."""
        doc = make_baseline_doc()
        del doc["entries"]["result_append"]
        existing = parse_doc(doc)
        runs = [{"result_append": make_fragment("result_append", v)} for v in (10.0, 10.1, 10.2)]
        candidate = fold(runs, existing=existing)
        assert candidate.entries["bar_lookup"] == existing.entries["bar_lookup"]
        assert candidate.entries["result_append"].value_ns_per_op == 10.0


class TestDumpAndPrBody:
    def test_dump_is_deterministic_with_trailing_newline(self) -> None:
        baseline = parse_doc(make_baseline_doc())
        first = bb.dump_baseline(baseline)
        assert first == bb.dump_baseline(baseline)
        assert first.endswith("}\n")
        assert json.loads(first)["hardware_key"] == KEY

    def test_pr_body_bootstrap(self) -> None:
        body = bb.render_pr_body(
            parse_doc(make_baseline_doc()), added=["bar_lookup"], replaced=[], run_url=RUN_URL
        )
        assert bb.BOOTSTRAP_DECISION_ID in body
        assert RUN_URL in body
        assert "close/reopen this PR" in body
        assert "GOVERNANCE" not in body

    def test_pr_body_replace_carries_governance_instruction(self) -> None:
        body = bb.render_pr_body(
            parse_doc(make_baseline_doc()),
            added=[],
            replaced=["bar_lookup"],
            run_url=RUN_URL,
        )
        assert "GOVERNANCE" in body
        assert "human-written docs/MIGRATION.md entry" in body
        assert "Carried unchanged" not in body

    def test_pr_body_names_carried_cells(self) -> None:
        body = bb.render_pr_body(
            parse_doc(make_baseline_doc()),
            added=[],
            replaced=["bar_lookup"],
            run_url=RUN_URL,
            carried=["dedup_bloom_probe", "result_append"],
        )
        assert "Carried unchanged" in body
        assert "dedup_bloom_probe, result_append" in body


# ────────────────────────────────────────────────────────────────────────
# validate_dir
# ────────────────────────────────────────────────────────────────────────


class TestValidateDir:
    def test_empty_dir_is_sound(self) -> None:
        assert bb.validate_dir({}) == ([], [])

    def test_readme_is_the_one_legal_non_json(self) -> None:
        errors, warnings = bb.validate_dir({"README.md": "docs"})
        assert errors == []
        assert warnings == []

    def test_stray_file_is_an_error(self) -> None:
        errors, _ = bb.validate_dir({"notes.txt": "hi"})
        assert errors and "stray" in errors[0]

    def test_schema_error_reported(self) -> None:
        errors, _ = bb.validate_dir({FILENAME: "{broken"})
        assert errors and "invalid JSON" in errors[0]

    def test_valid_file_no_errors(self) -> None:
        errors, warnings = bb.validate_dir({FILENAME: json.dumps(make_baseline_doc())})
        assert errors == []
        assert warnings == []

    def test_workload_hash_drift_is_warning_not_error(self) -> None:
        entries = {"bar_lookup": make_entry_doc("bar_lookup", workload_hash="sha256:" + "ef" * 32)}
        errors, warnings = bb.validate_dir(
            {FILENAME: json.dumps(make_baseline_doc(entries=entries))}
        )
        assert errors == []
        assert warnings and "S11" in warnings[0]


# ────────────────────────────────────────────────────────────────────────
# govern — the no-silent-re-baseline law
# ────────────────────────────────────────────────────────────────────────


def run_govern(
    changed: list[tuple[str, str]],
    *,
    base: Mapping[str, str] | None = None,
    head: Mapping[str, str] | None = None,
    migration: str = "",
    verifier: Any = run_ok,
    repo_slug: str = REPO_SLUG,
) -> bb.GovernReport:
    base_map = dict(base or {})
    head_map = dict(head or {})
    return bb.govern(
        changed=changed,
        base_reader=base_map.get,
        head_reader=head_map.get,
        migration_added_lines=migration,
        run_verifier=verifier,
        repo_slug=repo_slug,
    )


def baseline_text(entries: Mapping[str, Any] | None = None, **over: Any) -> str:
    return json.dumps(make_baseline_doc(entries=entries, **over))


class TestGovernFileLevel:
    def test_no_changes_ok(self) -> None:
        assert run_govern([]).ok

    @pytest.mark.parametrize("bad_slug", ["", "no-slash", "a/b/c", "a b/c"])
    def test_missing_or_malformed_repo_slug_fails_closed(self, bad_slug: str) -> None:
        """Review round 1 (BLOCKER): govern REQUIRES this repo's owner/repo
        slug — without the pin, a fork's completed bench run would satisfy
        the live verification. Fail-closed even with nothing changed."""
        with pytest.raises(bb.GovernanceError, match="repo_slug"):
            run_govern([], repo_slug=bad_slug)

    def test_readme_change_ungoverned(self) -> None:
        report = run_govern([("M", "README.md")])
        assert report.ok
        assert any("ungoverned" in note for note in report.notes)

    def test_stray_file_breach(self) -> None:
        report = run_govern([("A", "sneaky.txt")])
        assert not report.ok
        assert "stray" in report.breaches[0]

    def test_deleted_file_unledgered_breach(self) -> None:
        report = run_govern([("D", FILENAME)])
        assert not report.ok
        assert "DELETED without" in report.breaches[0]

    def test_deleted_file_ledgered_ok(self) -> None:
        report = run_govern([("D", FILENAME)], migration=f"retire {FILENAME} DEC-X-100")
        assert report.ok

    def test_unsupported_status_breach(self) -> None:
        report = run_govern([("R100", FILENAME)])
        assert not report.ok
        assert "unsupported git status" in report.breaches[0]

    def test_head_unreadable_breach(self) -> None:
        report = run_govern([("A", FILENAME)], head={})
        assert not report.ok
        assert "unreadable at HEAD" in report.breaches[0]

    def test_head_invalid_breach(self) -> None:
        report = run_govern([("A", FILENAME)], head={FILENAME: "{bad"})
        assert not report.ok
        assert "head baseline invalid" in report.breaches[0]

    def test_modified_missing_base_blob_breach(self) -> None:
        report = run_govern([("M", FILENAME)], head={FILENAME: baseline_text()})
        assert not report.ok
        assert "no readable base blob" in report.breaches[0]

    def test_modified_unparseable_base_breach(self) -> None:
        report = run_govern(
            [("M", FILENAME)], base={FILENAME: "{bad"}, head={FILENAME: baseline_text()}
        )
        assert not report.ok
        assert "base baseline unparseable" in report.breaches[0]


class TestGovernAddedCells:
    def test_added_file_bootstrap_ok(self) -> None:
        report = run_govern([("A", FILENAME)], head={FILENAME: baseline_text()})
        assert report.ok
        assert any("ADDED under" in note for note in report.notes)

    def test_added_file_unknown_id_breach(self) -> None:
        entries = {"bar_lookup": make_entry_doc("bar_lookup", decision_id=NEW_DEC)}
        report = run_govern([("A", FILENAME)], head={FILENAME: baseline_text(entries)})
        assert not report.ok
        assert "neither the" in report.breaches[0]

    def test_added_file_new_id_in_diff_ok(self) -> None:
        entries = {"bar_lookup": make_entry_doc("bar_lookup", decision_id=NEW_DEC)}
        report = run_govern(
            [("A", FILENAME)],
            head={FILENAME: baseline_text(entries)},
            migration=f"1522. **{NEW_DEC} — re-bless**",
        )
        assert report.ok

    def test_added_run_not_found_breach(self) -> None:
        report = run_govern(
            [("A", FILENAME)], head={FILENAME: baseline_text()}, verifier=lambda _url: None
        )
        assert not report.ok
        assert "not found on this repo" in report.breaches[0]

    def test_added_run_wrong_workflow_breach(self) -> None:
        report = run_govern(
            [("A", FILENAME)],
            head={FILENAME: baseline_text()},
            verifier=make_run_verifier(path=".github/workflows/ci.yml"),
        )
        assert not report.ok
        assert "not .github/workflows/bench-m4pro.yml" in report.breaches[0]

    def test_added_run_not_completed_breach(self) -> None:
        report = run_govern(
            [("A", FILENAME)],
            head={FILENAME: baseline_text()},
            verifier=make_run_verifier(status="in_progress"),
        )
        assert not report.ok
        assert "!= completed" in report.breaches[0]

    def test_added_run_failed_conclusion_breach(self) -> None:
        """Review round 1: a completed-but-FAILED bench run is not provenance."""
        report = run_govern(
            [("A", FILENAME)],
            head={FILENAME: baseline_text()},
            verifier=make_run_verifier(conclusion="failure"),
        )
        assert not report.ok
        assert "!= success" in report.breaches[0]

    def test_added_foreign_repo_run_url_breach_without_api_query(self) -> None:
        """Review round 1 (BLOCKER): a run on an attacker-controlled repo —
        which keeps bench-m4pro.yml at the required path with a chosen
        head_sha — is breached on the slug alone; the (would-be-confirming)
        verifier is never even consulted."""
        calls: list[str] = []

        def confirming_spy(url: str) -> Mapping[str, object]:
            calls.append(url)
            return dict(run_ok(url))

        foreign = make_provenance(run_url="https://github.com/evil-attacker/bruteX/actions/runs/42")
        entries = {"bar_lookup": make_entry_doc("bar_lookup", provenance=foreign)}
        report = run_govern(
            [("A", FILENAME)],
            head={FILENAME: baseline_text(entries)},
            verifier=confirming_spy,
        )
        assert not report.ok
        assert "foreign repo" in report.breaches[0]
        assert "evil-attacker/bruteX" in report.breaches[0]
        assert calls == []

    def test_added_run_wrong_head_sha_breach(self) -> None:
        report = run_govern(
            [("A", FILENAME)],
            head={FILENAME: baseline_text()},
            verifier=make_run_verifier(head_sha="f" * 40),
        )
        assert not report.ok
        assert "head_sha" in report.breaches[0]


def _modified_pair(
    head_over: Mapping[str, Any], base_over: Mapping[str, Any] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """(base, head) file maps with bar_lookup modified per the overrides."""
    base_entries = {"bar_lookup": make_entry_doc("bar_lookup", **(dict(base_over or {})))}
    head_entries = {"bar_lookup": make_entry_doc("bar_lookup", **dict(head_over))}
    return (
        {FILENAME: baseline_text(base_entries)},
        {FILENAME: baseline_text(head_entries)},
    )


class TestGovernModifiedCells:
    """The S7 MODIFIED-cell grid: silent bumps are impossible."""

    HAPPY_HEAD: ClassVar[dict[str, Any]] = {
        "value_ns_per_op": 55.0,
        "samples_ns_per_op": [55.0, 56.0, 57.0],
        "decision_id": NEW_DEC,
        "provenance": make_provenance(run_url=RUN_URL_2, commit=COMMIT),
    }
    HAPPY_MIGRATION = (
        f"1522. **{NEW_DEC} — re-bless bar_lookup to 55.0 after the measured kernel win**"
    )

    def test_bare_value_bump_no_ledger_breach(self) -> None:
        base, head = _modified_pair(
            {"value_ns_per_op": 55.0, "samples_ns_per_op": [55.0, 56.0, 57.0]}
        )
        report = run_govern([("M", FILENAME)], base=base, head=head)
        assert not report.ok
        assert "bootstrap" in report.breaches[0]

    def test_bump_with_base_equal_id_breach(self) -> None:
        base, head = _modified_pair(
            {
                "value_ns_per_op": 55.0,
                "samples_ns_per_op": [55.0, 56.0, 57.0],
                "decision_id": NEW_DEC,
            },
            base_over={"decision_id": NEW_DEC},
        )
        report = run_govern([("M", FILENAME)], base=base, head=head, migration=self.HAPPY_MIGRATION)
        assert not report.ok
        assert "reuses the base decision id" in report.breaches[0]

    def test_bump_id_not_in_diff_breach(self) -> None:
        base, head = _modified_pair(self.HAPPY_HEAD)
        report = run_govern([("M", FILENAME)], base=base, head=head)
        assert not report.ok
        assert "not in this PR's MIGRATION.md added lines" in report.breaches[0]

    def test_bump_missing_value_token_breach(self) -> None:
        """Ledger names the id + entry but NOT the literal 55.0 token."""
        base, head = _modified_pair(self.HAPPY_HEAD)
        report = run_govern(
            [("M", FILENAME)],
            base=base,
            head=head,
            migration=f"1522. **{NEW_DEC} — re-bless bar_lookup (value elided)**",
        )
        assert not report.ok
        assert "'55.0'" in report.breaches[0]

    def test_bump_missing_entry_key_token_breach(self) -> None:
        base, head = _modified_pair(self.HAPPY_HEAD)
        report = run_govern(
            [("M", FILENAME)],
            base=base,
            head=head,
            migration=f"1522. **{NEW_DEC} — re-bless to 55.0**",
        )
        assert not report.ok
        assert "'bar_lookup'" in report.breaches[0]

    def test_bump_reused_run_url_breach(self) -> None:
        head_over = dict(self.HAPPY_HEAD, provenance=make_provenance())
        base, head = _modified_pair(head_over)
        report = run_govern([("M", FILENAME)], base=base, head=head, migration=self.HAPPY_MIGRATION)
        assert not report.ok
        assert "FRESH measured run" in report.breaches[0]

    def test_bump_forged_run_breach(self) -> None:
        base, head = _modified_pair(self.HAPPY_HEAD)
        report = run_govern(
            [("M", FILENAME)],
            base=base,
            head=head,
            migration=self.HAPPY_MIGRATION,
            verifier=lambda _url: None,
        )
        assert not report.ok
        assert "not found" in report.breaches[0]

    def test_bump_full_happy_path_ok(self) -> None:
        base, head = _modified_pair(self.HAPPY_HEAD)
        report = run_govern([("M", FILENAME)], base=base, head=head, migration=self.HAPPY_MIGRATION)
        assert report.ok
        assert any("MODIFIED under" in note for note in report.notes)

    def test_unchanged_cells_ok_without_ledger(self) -> None:
        base = {FILENAME: baseline_text()}
        report = run_govern([("M", FILENAME)], base=base, head=dict(base))
        assert report.ok

    def test_added_cell_in_modified_file_bootstrap_ok(self) -> None:
        base_entries = {"bar_lookup": make_entry_doc("bar_lookup")}
        head_entries = {
            "bar_lookup": make_entry_doc("bar_lookup"),
            "result_append": make_entry_doc("result_append"),
        }
        report = run_govern(
            [("M", FILENAME)],
            base={FILENAME: baseline_text(base_entries)},
            head={FILENAME: baseline_text(head_entries)},
        )
        assert report.ok

    def test_removed_cell_unmentioned_breach(self) -> None:
        base_entries = {
            "bar_lookup": make_entry_doc("bar_lookup"),
            "result_append": make_entry_doc("result_append"),
        }
        head_entries = {"bar_lookup": make_entry_doc("bar_lookup")}
        report = run_govern(
            [("M", FILENAME)],
            base={FILENAME: baseline_text(base_entries)},
            head={FILENAME: baseline_text(head_entries)},
        )
        assert not report.ok
        assert "REMOVED without" in report.breaches[0]

    def test_removed_cell_mentioned_ok(self) -> None:
        base_entries = {
            "bar_lookup": make_entry_doc("bar_lookup"),
            "result_append": make_entry_doc("result_append"),
        }
        head_entries = {"bar_lookup": make_entry_doc("bar_lookup")}
        report = run_govern(
            [("M", FILENAME)],
            base={FILENAME: baseline_text(base_entries)},
            head={FILENAME: baseline_text(head_entries)},
            migration=f"1522. **{NEW_DEC} — workload change removes result_append cell**",
        )
        assert report.ok

    def test_top_level_tamper_unledgered_breach(self) -> None:
        base = {FILENAME: baseline_text()}
        head = {FILENAME: baseline_text(context={**CONTEXT, "python": "3.13.0"})}
        report = run_govern([("M", FILENAME)], base=base, head=head)
        assert not report.ok
        assert "top-level fingerprint/context/hardware_key" in report.breaches[0]

    def test_top_level_change_ledgered_ok(self) -> None:
        base = {FILENAME: baseline_text()}
        head = {FILENAME: baseline_text(context={**CONTEXT, "python": "3.13.0"})}
        report = run_govern(
            [("M", FILENAME)],
            base=base,
            head=head,
            migration=f"1522. **{NEW_DEC} — python 3.13 context re-bless**",
        )
        assert report.ok

    def test_malformed_run_url_in_cell_breach(self) -> None:
        """_verify_cell_run's own URL screen (direct — parse blocks this shape)."""
        breaches: list[str] = []
        good = parse_doc(make_baseline_doc()).entries["bar_lookup"]
        bad_prov = bb.Provenance(
            run_url="https://nope",
            commit=COMMIT,
            captured_utc="2026-07-06T00:00:00Z",
            runner_name="t",
        )
        cell = bb.EntryRecord(
            value_ns_per_op=good.value_ns_per_op,
            samples_ns_per_op=good.samples_ns_per_op,
            spread_pct=good.spread_pct,
            tolerance_pct=good.tolerance_pct,
            improvement_warn_ratio=good.improvement_warn_ratio,
            suspicious_speedup_ratio=good.suspicious_speedup_ratio,
            workload_hash=good.workload_hash,
            decision_id=good.decision_id,
            provenance=bad_prov,
        )
        bb._verify_cell_run(
            "bar_lookup", cell, repo_slug=REPO_SLUG, run_verifier=run_ok, breaches=breaches
        )
        assert breaches and "malformed" in breaches[0]
