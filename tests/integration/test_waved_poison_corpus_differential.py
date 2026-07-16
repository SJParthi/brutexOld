"""Wave-D poison-corpus DIFFERENTIAL harness (ADR-0020 slice G9).

DEC-LAKE-RUST-WAVED-HARNESS-001 (MIGRATION.md #1916). The runner drives TWO
scanner implementations over INDEPENDENT fresh builds of the SAME poison
case (`tests/fakes/poison_corpus.py`) and asserts NORMALIZED verdict
equality — the mechanical gate the future `lake_invariants`/`lake_heal`
Rust flip must pass (`docs/runbooks/wave-d-poison-corpus.md`).

THIS SLICE ships the Python-vs-Python IDENTITY self-test only (no Rust twin
exists for these modules yet): ``impl_a = impl_b = scan_lake_invariants``
over two fresh builds — which also pins per-case DETERMINISM (no clock /
dict-order / absolute-path leak survives the normalization). The per-case
EXPECTATION test is the anti-vacuous tooth: an empty corpus, a wrong
builder, or a scanner regression fails loudly against the manifest's
``expected_finding_types`` / ``expects_skip``.

Normalization law: ``duration_s`` (wall clock) is EXCLUDED; every absolute
lake-root prefix folds to ``<LAKE>``; findings become sorted
``(type-name, field-dict)`` tuples; the census rides along with its
wall-clock keys dropped. Log TEXT is deliberately NOT compared (implementation
detail — the readiness doc §3).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.data import lake_invariants as li
from brutex.data.lake_invariants import (
    LakeScanReport,
    build_lake_census,
    census_to_dict,
    scan_lake_invariants,
)
from tests.fakes.poison_corpus import CORPUS, PoisonCase, supports_symlink

#: Any callable honoring the ``scan_lake_invariants`` signature (the future
#: Rust-backed twin plugs in here).
ScannerImpl = Callable[..., LakeScanReport]

#: The §1 in-scope ON-DISK poison classes the corpus MUST cover (the
#: runtime-injection classes — mid-heal TOCTOU swap, PanicException at L3 —
#: are deliberately out-of-corpus; see the readiness doc §5).
_REGISTERED_IN_SCOPE_CLASSES: frozenset[str] = frozenset(
    {
        "bars_intra_file_dup",
        "bars_foreign_month_pure",
        "bars_foreign_month_overlap",
        "legacy_key_drift_sibling",
        "option_intra_file_dup",
        "option_foreign_month_pure",
        "option_foreign_month_overlap",
        "option_multi_month_overlap",
        "symlinked_series_dir",
        "symlinked_options_root",
        "symlinked_partition_file",
        "symlink_loop_partition_file",
        "dangling_symlink_option_file",
        "glob_metachar_series",
        "option_nested_dtype_list_side",
        "option_out_of_range_datetime",
        "bars_out_of_range_datetime",
        "bars_naive_timestamp_routing",
        "bars_zero_row_file",
        "bars_schema_alien_missing_columns",
        "bars_price_sanity_rows",
        "option_non_canonical_stem",
        "option_unicode_digit_filename",
        "option_null_side_foreign_key",
        "option_mass_poison_majority",
    }
)


# ────────────────────────────────────────────────────────────────────────
# Verdict normalization
# ────────────────────────────────────────────────────────────────────────


def _normalize(value: object, root: str) -> object:
    """Fold a verdict fragment into a lake-root-independent comparable shape."""
    if isinstance(value, Path):
        return str(value).replace(root, "<LAKE>")
    if isinstance(value, str):
        return value.replace(root, "<LAKE>")
    if isinstance(value, Mapping):
        return {str(k): _normalize(v, root) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return tuple(_normalize(v, root) for v in value)
    return value


def _drop_wall_clock(value: object) -> object:
    """Recursively drop wall-clock keys (``duration_s`` / ``*_utc``) from mappings."""
    if isinstance(value, Mapping):
        return {
            str(k): _drop_wall_clock(v)
            for k, v in value.items()
            if not (str(k) == "duration_s" or str(k).endswith("_utc"))
        }
    if isinstance(value, (list, tuple)):
        return tuple(_drop_wall_clock(v) for v in value)
    return value


def scan_verdict(report: LakeScanReport, lake_root: Path) -> dict[str, object]:
    """The comparable scan verdict: counters + type-tagged findings + census.

    ``duration_s`` is EXCLUDED (wall clock); every absolute lake-root prefix
    folds to ``<LAKE>`` so two lakes at different tmp roots compare equal.
    """
    root = str(lake_root)
    raw = dataclasses.asdict(report)
    raw.pop("duration_s")
    raw.pop("findings")
    verdict: dict[str, object] = {key: _normalize(val, root) for key, val in raw.items()}
    findings = tuple(
        (type(finding).__name__, _normalize(dataclasses.asdict(finding), root))
        for finding in report.findings
    )
    verdict["findings"] = tuple(sorted(findings, key=repr))
    census = census_to_dict(build_lake_census(report), mode="observe")
    verdict["census"] = _normalize(_drop_wall_clock(census), root)
    return verdict


def run_differential(
    impl_a: ScannerImpl, impl_b: ScannerImpl, case: PoisonCase, tmp: Path
) -> tuple[dict[str, object], dict[str, object]]:
    """Build TWO INDEPENDENT fresh lakes for *case* (a heal-capable impl may
    mutate its lake), run one impl over each, return the normalized verdicts.
    """
    verdicts: list[dict[str, object]] = []
    for label, impl in (("impl-a", impl_a), ("impl-b", impl_b)):
        lake = tmp / label
        lake.mkdir()
        case.build(lake)
        report = impl(lake, **dict(case.scan_kwargs))
        verdicts.append(scan_verdict(report, lake))
    return verdicts[0], verdicts[1]


def _skip_unless_supported(case: PoisonCase, base: Path) -> None:
    """Honor ``PoisonCase.requires`` (unknown tokens fail LOUD, never silently pass)."""
    if case.requires is None:
        return
    if case.requires != "symlink":
        raise ValueError(f"unknown PoisonCase.requires token: {case.requires!r}")
    if not supports_symlink(base):
        pytest.skip("filesystem does not support symlinks")


# ────────────────────────────────────────────────────────────────────────
# The differential + expectation teeth
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: str(case.name))
class TestPoisonCorpusDifferential:
    def test_python_identity(self, case: PoisonCase, tmp_path: Path) -> None:
        """impl_a = impl_b = the Python oracle over two fresh builds → EQUAL."""
        _skip_unless_supported(case, tmp_path)
        verdict_a, verdict_b = run_differential(
            scan_lake_invariants, scan_lake_invariants, case, tmp_path
        )
        assert verdict_a == verdict_b

    def test_expected_verdict(self, case: PoisonCase, tmp_path: Path) -> None:
        """Anti-vacuous tooth: the built lake yields EXACTLY the manifest verdict."""
        _skip_unless_supported(case, tmp_path)
        lake = tmp_path / "lake"
        lake.mkdir()
        case.build(lake)
        report = scan_lake_invariants(lake, **dict(case.scan_kwargs))
        got_types = {type(finding).__name__ for finding in report.findings}
        assert got_types == set(case.expected_finding_types)
        if case.expects_skip:
            assert report.files_skipped + report.option_files_skipped > 0


# ────────────────────────────────────────────────────────────────────────
# Corpus/manifest integrity
# ────────────────────────────────────────────────────────────────────────


def test_corpus_covers_registered_classes() -> None:
    """The corpus names are unique and ⊇ the §1 in-scope on-disk class list."""
    names = [case.name for case in CORPUS]
    assert len(names) == len(set(names))
    missing = _REGISTERED_IN_SCOPE_CLASSES - set(names)
    assert not missing, f"corpus is missing registered poison classes: {sorted(missing)}"


def test_expected_types_are_real_finding_classes() -> None:
    """Every expected finding type names a REAL exported scanner finding class."""
    for case in CORPUS:
        for type_name in case.expected_finding_types:
            assert isinstance(getattr(li, type_name), type), (case.name, type_name)


def test_skip_unless_supported_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unknown requires-token fails LOUD; an unsupported capability skips."""
    bogus = dataclasses.replace(CORPUS[0], requires="fifo")
    with pytest.raises(ValueError, match="unknown PoisonCase.requires token"):
        _skip_unless_supported(bogus, tmp_path)
    needs_symlink = dataclasses.replace(CORPUS[0], requires="symlink")
    monkeypatch.setattr(
        "tests.integration.test_waved_poison_corpus_differential.supports_symlink",
        lambda base: False,
    )
    with pytest.raises(pytest.skip.Exception):
        _skip_unless_supported(needs_symlink, tmp_path)


def test_supports_symlink_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The capability probe: True on a symlink-capable fs, False on OSError."""
    assert supports_symlink(tmp_path) is True
    assert not (tmp_path / ".symlink-probe").exists()  # probe cleans up

    def _refuse(target: object, link: object) -> None:
        raise OSError("symlinks unsupported")

    import tests.fakes.poison_corpus as corpus_module

    monkeypatch.setattr(corpus_module.os, "symlink", _refuse)
    assert supports_symlink(tmp_path) is False


def test_drop_wall_clock_unit() -> None:
    """Wall-clock keys are dropped recursively; every other shape passes through."""
    folded = _drop_wall_clock(
        {
            "duration_s": 1.5,
            "last_heal_utc": "2026-07-16T00:00:00Z",
            "files": 3,
            "nested": [{"duration_s": 2.0, "clean": 1}],
        }
    )
    assert folded == {"files": 3, "nested": ({"clean": 1},)}
