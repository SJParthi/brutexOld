"""R16/D-6 mechanized traceability matrix (DEC-SWEEP-UNKILLABLE-CORE-001 WP-6).

Every Phase-2 requirement/gap token — R7 R16 R28 R38 R39 R40 R41 R42 R43 R45 +
G1..G15 — must map to at least ONE collected test BY NAME, from two sources:

1. **``TRACEABILITY`` dicts** (the Phase-2 convention): a module-level
   ``TRACEABILITY: dict[test_name, (tokens...)]`` in a test file. Every KEY
   must name a test function that actually exists in that file — a renamed or
   deleted test breaks its own mapping LOUDLY (never a stale row).
2. **Name prefixes** (graft C): ``test_rNN_*`` / ``test_gNN_*`` function names
   inside the Phase-2 file set (zero-padded forms like ``test_g03_`` normalize
   to ``G3``). The scan is SCOPED to files that carry a ``TRACEABILITY`` dict
   plus the explicit ``_EXTRA_PHASE2_FILES`` list — foreign suites that happen
   to use ``r1``/``r2`` names (pivot levels!) can never satisfy a token.

The matrix FAILS when any token loses its last test — proven here by the teeth
tests that manipulate the collected set (D-6: "prove by temporary rename,
leave the proof as a test that manipulates the collection set").
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path
from typing import Final

import pytest

#: This file's own mapping (the matrix gate itself carries the R16 token).
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r16_every_required_token_maps_to_a_collected_test": ("R16",),
    "test_r16_matrix_goes_red_when_any_token_loses_its_last_test": ("R16",),
    "test_r16_stale_traceability_key_fails_loudly": ("R16",),
    "test_r16_name_prefix_normalization_exact": ("R16",),
    "test_r16_foreign_prefixed_names_are_out_of_scope": ("R16",),
    "test_r16_collector_output_exact_on_a_synthetic_file": ("R16",),
}

#: The Phase-2 scope (WP-6 acceptance list, verbatim).
REQUIRED_TOKENS: Final[frozenset[str]] = frozenset(
    {"R7", "R16", "R28", "R38", "R39", "R40", "R41", "R42", "R43", "R45"}
    | {f"G{i}" for i in range(1, 16)}
)

#: Phase-2 test files WITHOUT a TRACEABILITY dict whose rNN/gNN-prefixed test
#: names still count (WP-5 shipped these before the dict convention landed).
_EXTRA_PHASE2_FILES: Final[tuple[str, ...]] = (
    "tests/integration/test_sweep_progress_enrichment.py",
    "tests/integration/test_sweep_runcore_byte_identity.py",
    "tests/integration/test_sweep_detached_survival.py",
    "tests/golden/test_runcore_golden_intact.py",
)

_TESTS_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^test_([rg])0*(\d+)_")


def _normalize_prefix(name: str) -> str | None:
    """``test_g03_x`` → ``G3``; ``test_r38_x`` → ``R38``; non-matching → None."""
    match = _PREFIX_RE.match(name)
    if match is None:
        return None
    return f"{match.group(1).upper()}{int(match.group(2))}"


def _module_traceability(tree: ast.Module) -> dict[str, tuple[str, ...]] | None:
    """The module-level ``TRACEABILITY`` literal, or ``None`` when absent."""
    for node in tree.body:
        target: str | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        if target == "TRACEABILITY" and value is not None:
            literal = ast.literal_eval(value)
            if not isinstance(literal, dict):  # pragma: no cover — convention guard
                raise TypeError(f"TRACEABILITY must be a dict literal, got {type(literal)}")
            return {str(k): tuple(str(t) for t in v) for k, v in literal.items()}
    return None


def _test_names(tree: ast.Module) -> frozenset[str]:
    """Every test function DEFINED in the module (top level + inside classes)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            names.add(node.name)
    return frozenset(names)


@functools.lru_cache(maxsize=4)
def collect_token_map(tests_root: Path) -> dict[str, tuple[str, ...]]:
    """token → sorted ``"<relpath>::<test_name>"`` entries over the Phase-2 scope.

    Raises
    ------
    ValueError
        When a ``TRACEABILITY`` key names a test that does NOT exist in its
        file — a stale mapping is a LOUD failure, never a silent pass.
    """
    extras = {tests_root.parent / rel for rel in _EXTRA_PHASE2_FILES}
    token_map: dict[str, list[str]] = {}
    for path in sorted(tests_root.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mapping = _module_traceability(tree)
        if mapping is None and path not in extras:
            continue  # out of Phase-2 scope — foreign prefixes never count
        defined = _test_names(tree)
        rel = path.relative_to(tests_root.parent).as_posix()
        if mapping is not None:
            stale = sorted(set(mapping) - defined)
            if stale:
                raise ValueError(
                    f"{rel}: TRACEABILITY keys name no collected test: {stale} "
                    f"(renamed/deleted test left a stale mapping)"
                )
            for name, tokens in mapping.items():
                for token in tokens:
                    token_map.setdefault(token.upper(), []).append(f"{rel}::{name}")
        for name in defined:
            prefix_token = _normalize_prefix(name)
            if prefix_token is not None:
                token_map.setdefault(prefix_token, []).append(f"{rel}::{name}")
    return {token: tuple(sorted(set(entries))) for token, entries in token_map.items()}


def missing_tokens(token_map: dict[str, tuple[str, ...]]) -> frozenset[str]:
    """The required tokens with ZERO mapped tests (the matrix's red condition)."""
    return frozenset(token for token in REQUIRED_TOKENS if not token_map.get(token))


# ────── the gate ──────


def test_r16_every_required_token_maps_to_a_collected_test() -> None:
    token_map = collect_token_map(_TESTS_ROOT)
    missing = missing_tokens(token_map)
    assert missing == frozenset(), (
        f"Phase-2 tokens with NO named test: {sorted(missing)} — "
        f"map them via a TRACEABILITY dict or a test_rNN_/test_gNN_ name"
    )


# ────── the teeth (D-6: manipulate the collection set) ──────


@pytest.mark.parametrize("token", sorted(REQUIRED_TOKENS))
def test_r16_matrix_goes_red_when_any_token_loses_its_last_test(token: str) -> None:
    """Dropping ANY required token's entries flips the matrix red — every token
    is genuinely load-bearing (the temporary-rename proof, mechanized)."""
    token_map = dict(collect_token_map(_TESTS_ROOT))
    assert token_map.get(token), f"{token} must be mapped before the teeth can bite"
    del token_map[token]
    assert missing_tokens(token_map) == frozenset({token})


def test_r16_stale_traceability_key_fails_loudly(tmp_path: Path) -> None:
    """A TRACEABILITY key whose test was renamed/deleted is a LOUD ValueError."""
    scoped = tmp_path / "tests"
    scoped.mkdir()
    (tmp_path / "conftest.py").touch()
    stale = scoped / "test_stale_map.py"
    stale.write_text(
        'TRACEABILITY = {"test_r7_gone": ("R7",)}\n\ndef test_r7_renamed() -> None: ...\n'
    )
    with pytest.raises(ValueError, match=r"test_stale_map\.py.*test_r7_gone"):
        collect_token_map(scoped)


def test_r16_name_prefix_normalization_exact() -> None:
    assert _normalize_prefix("test_g03_parallel_progress") == "G3"
    assert _normalize_prefix("test_g15_beats") == "G15"
    assert _normalize_prefix("test_r38_rehearsal") == "R38"
    assert _normalize_prefix("test_r7_detached") == "R7"
    assert _normalize_prefix("test_g0_zero") == "G0"
    assert _normalize_prefix("test_gap_not_a_token") is None
    assert _normalize_prefix("test_run_plain") is None
    assert _normalize_prefix("helper_r7_not_a_test") is None


def test_r16_foreign_prefixed_names_are_out_of_scope(tmp_path: Path) -> None:
    """A foreign suite's ``test_r1_*`` (pivot levels!) never satisfies a token:
    only TRACEABILITY-carrying / explicitly-listed files are scanned."""
    scoped = tmp_path / "tests"
    scoped.mkdir()
    foreign = scoped / "test_pivots_foreign.py"
    foreign.write_text("def test_r1_matches_screenshot() -> None: ...\n")
    assert collect_token_map(scoped) == {}


def test_r16_collector_output_exact_on_a_synthetic_file(tmp_path: Path) -> None:
    scoped = tmp_path / "tests"
    scoped.mkdir()
    module = scoped / "test_synthetic_phase2.py"
    module.write_text(
        "TRACEABILITY = {\n"
        '    "test_alpha": ("R42", "G6"),\n'
        '    "test_g03_named": ("G3",),\n'
        "}\n\n"
        "def test_alpha() -> None: ...\n\n"
        "class TestGroup:\n"
        "    def test_g03_named(self) -> None: ...\n"
    )
    token_map = collect_token_map(scoped)
    rel = "tests/test_synthetic_phase2.py"
    assert token_map == {
        "R42": (f"{rel}::test_alpha",),
        "G6": (f"{rel}::test_alpha",),
        "G3": (f"{rel}::test_g03_named",),
    }
