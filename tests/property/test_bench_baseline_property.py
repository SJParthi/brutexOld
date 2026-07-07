"""Hypothesis invariants for the bench-baseline core (DEC-BENCH-ABS-BASELINE-001).

Pinned laws: dump→parse round-trip identity (Golden Rule #7 idempotence);
measured==blessed is always OK; verdict monotonicity in the measured value
(designed to kill ``>``/``>=`` and sign-flip mutants); the tolerance cap
keeps a 2x slowdown failing under EVERY governable tolerance — property-run
over the WHOLE ``[0, 100)`` cap range including the IEEE boundary where
``1 + t/100`` rounds to exactly 2.0 (the schema REJECTS it float-exactly,
review round 2); fold value ∈ [min, max] of all samples; every generated
fingerprint renders a legal slug key; govern is deterministic under
input-order shuffles.
"""

from __future__ import annotations

import json
import math

import pytest
from brutex.observability import bench_baseline as bb
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.unit.test_bench_baseline import (
    FILENAME,
    KEY,
    all_fragments,
    capture_runs,
    fold,
    make_baseline_doc,
    make_entry_doc,
    make_fragment,
    parse_doc,
    run_govern,
)

_VALUES = st.floats(min_value=1e-3, max_value=1e9, allow_nan=False, allow_infinity=False)
#: The WHOLE structural cap range [0, 100) — hypothesis reaches the true max
#: legal double (math.nextafter(100, 0)), whose failure multiplier rounds to
#: exactly 2.0 and which the schema must therefore REJECT (review round 2).
_TOLERANCES_FULL_CAP_RANGE = st.floats(
    min_value=0.0, max_value=100.0, exclude_max=True, allow_nan=False
)
#: Schema-LEGAL tolerances only (failure multiplier float-exactly < 2.0).
_TOLERANCES = _TOLERANCES_FULL_CAP_RANGE.filter(
    lambda t: bb.tolerance_multiplier(t) < bb.TOLERANCE_MULTIPLIER_MAX_EXCLUSIVE
)


def _baseline_with(value: float, tolerance: float = 50.0) -> bb.Baseline:
    entries = {
        entry: make_entry_doc(
            entry,
            value_ns_per_op=value,
            samples_ns_per_op=[value, value * 1.5, value * 1.25],
            tolerance_pct=tolerance,
        )
        for entry in sorted(bb.REQUIRED_ENTRY_KEYS)
    }
    return parse_doc(make_baseline_doc(entries=entries))


def _verdict_for(measured: float, blessed: float, tolerance: float = 50.0) -> bb.Verdict:
    baseline = _baseline_with(blessed, tolerance)
    fragments = all_fragments()
    fragments["bar_lookup"] = make_fragment("bar_lookup", measured)
    report = bb.compare(baseline, fragments, runtime_key=KEY, runtime_python="3.12.8")
    return next(v.verdict for v in report.verdicts if v.entry == "bar_lookup")


class TestRoundTrip:
    @given(value=_VALUES, tolerance=_TOLERANCES)
    @settings(max_examples=40, deadline=None)
    def test_dump_parse_round_trip_identity(self, value: float, tolerance: float) -> None:
        baseline = _baseline_with(value, tolerance)
        dumped = bb.dump_baseline(baseline)
        assert bb.parse_baseline(dumped, filename=FILENAME) == baseline
        # Byte-stable: a second dump of the re-parsed doc is identical.
        assert bb.dump_baseline(bb.parse_baseline(dumped, filename=FILENAME)) == dumped


class TestVerdictLaws:
    @given(value=_VALUES, tolerance=_TOLERANCES)
    @settings(max_examples=40, deadline=None)
    def test_measured_equals_blessed_is_ok(self, value: float, tolerance: float) -> None:
        assert _verdict_for(value, value, tolerance) is bb.Verdict.OK

    @given(blessed=_VALUES, tolerance=_TOLERANCES_FULL_CAP_RANGE)
    @settings(max_examples=40, deadline=None)
    def test_two_x_slowdown_fails_under_every_tolerance(
        self, blessed: float, tolerance: float
    ) -> None:
        """The S6 structural guarantee over the WHOLE cap range: every
        tolerance in [0, 100) either parses (and then an exact 2x slowdown
        FAILS) or is schema-REJECTED because its failure multiplier rounds
        to exactly 2.0 in IEEE double (the float-boundary hole, review
        round 2 — no legal config can pass a 2x slowdown)."""
        if not 1.0 + tolerance / 100.0 < 2.0:  # raw IEEE math — the independent oracle
            with pytest.raises(bb.BaselineSchemaError, match="tolerance_pct must be in"):
                _baseline_with(1.0, tolerance)
            return
        assert _verdict_for(blessed * 2.0, blessed, tolerance) is bb.Verdict.REGRESSION

    @given(blessed=_VALUES, factor=st.floats(min_value=1.51, max_value=64.0, allow_nan=False))
    @settings(max_examples=40, deadline=None)
    def test_regression_monotone_in_measured(self, blessed: float, factor: float) -> None:
        """Above the default 1.5x threshold, larger measured never flips back OK."""
        assert _verdict_for(blessed * factor, blessed) is bb.Verdict.REGRESSION

    @given(blessed=_VALUES, factor=st.floats(min_value=4.1, max_value=1000.0, allow_nan=False))
    @settings(max_examples=40, deadline=None)
    def test_suspicious_monotone_in_speedup(self, blessed: float, factor: float) -> None:
        """Beyond 4x faster, a smaller measured never flips back OK."""
        assert _verdict_for(blessed / factor, blessed) is bb.Verdict.SUSPICIOUS_SPEEDUP

    @given(blessed=st.floats(min_value=1.0, max_value=1e6, allow_nan=False))
    @settings(max_examples=40, deadline=None)
    def test_threshold_boundary_passes_exactly(self, blessed: float) -> None:
        """measured == blessed x 1.5 passes (strict >) for every blessed value."""
        measured = blessed * 1.5
        if not math.isfinite(measured):  # pragma: no cover — bounded strategy
            return
        assert _verdict_for(measured, blessed) in (
            bb.Verdict.OK,
            bb.Verdict.OK_IMPROVEMENT_WARN,
        )


class TestFoldLaws:
    @given(
        values=st.tuples(
            st.floats(min_value=10.0, max_value=11.0, allow_nan=False),
            st.floats(min_value=10.0, max_value=11.0, allow_nan=False),
            st.floats(min_value=10.0, max_value=11.0, allow_nan=False),
        )
    )
    @settings(max_examples=25, deadline=None)
    def test_fold_value_is_min_of_all_samples(self, values: tuple[float, float, float]) -> None:
        candidate = fold(capture_runs(values))
        for cell in candidate.entries.values():
            assert cell.value_ns_per_op == min(cell.samples_ns_per_op)
            assert (
                min(cell.samples_ns_per_op) <= cell.value_ns_per_op <= max(cell.samples_ns_per_op)
            )
            assert cell.spread_pct >= 0.0


class TestKeyLaws:
    @given(
        model=st.text(min_size=0, max_size=24),
        physical=st.integers(min_value=1, max_value=256),
        performance=st.integers(min_value=1, max_value=256),
        efficiency=st.integers(min_value=0, max_value=256),
        ram=st.integers(min_value=0, max_value=4096),
        arch=st.text(min_size=0, max_size=12),
    )
    @settings(max_examples=60, deadline=None)
    def test_every_generated_fingerprint_yields_a_legal_key(
        self,
        model: str,
        physical: int,
        performance: int,
        efficiency: int,
        ram: int,
        arch: str,
    ) -> None:
        key = bb.format_hardware_key(
            model_slug=bb.slugify(model),
            physical_cores=physical,
            performance_cores=performance,
            efficiency_cores=efficiency,
            total_ram_gib=ram,
            arch=bb.slugify(arch),
        )
        assert bb.KEY_SLUG_RE.fullmatch(key), key


class TestGovernDeterminism:
    @given(seed=st.randoms(use_true_random=False))
    @settings(max_examples=15, deadline=None)
    def test_govern_deterministic_over_shuffled_inputs(self, seed) -> None:  # type: ignore[no-untyped-def]
        changed = [
            ("A", FILENAME),
            ("D", "old-box-2c2p0e-4g-arm64.json"),
            ("A", "stray.txt"),
        ]
        head = {FILENAME: json.dumps(make_baseline_doc())}
        baseline_report = run_govern(list(changed), head=head)
        shuffled = list(changed)
        seed.shuffle(shuffled)
        report = run_govern(shuffled, head=head)
        assert report.ok == baseline_report.ok
        assert sorted(report.breaches) == sorted(baseline_report.breaches)
