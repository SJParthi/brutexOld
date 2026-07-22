"""The frozen ``brutex.ranking.tiers`` Rust-seam NO-GO contract.

The MECHANIZED no-go evidence for DEC-RANK-TIERS-RUST-NOGO-001 (recorded in
:mod:`brutex.ranking.tiers`'s module docstring + the ledger entry), mirroring
the ``brutex/models.py`` type-port precedent
(:mod:`tests.unit.test_models_type_contract`, DEC-CORE-RUST-MODELS-001):
every pin here freezes a property the reasoned NO-GO rests on, so a future
port attempt must consciously confront this contract instead of silently
eroding its premises.

The five pin families:

(a) **Module boundary** — stdlib-only imports at EVERY scope, no dynamic
    import / wheel reference, no Rust-named module attribute, and the
    docstring carries the verdict + the verbatim ADR-0020 banner.
(b) **Reason vocabulary** — ``TIER_REASON_ORDER`` / ``MISSING_INPUT_PREFIX``
    / ``TIER_LABELS`` frozen byte-for-byte: the reason strings are MESSAGE
    BYTES a kernel would have to reproduce exactly.
(c) **Decision surface** — ``TierInputs``/``TierVerdict`` field order + the
    load-bearing Optional-field taxonomy frozen (the ``None`` plane any FFI
    boundary would have to encode).
(d) **Cold-path consumers** — the frozen production-importer set holds the
    "at most once per emitted top-100 entry, never the mask hot path"
    cardinality claim structurally: a NEW consumer (especially a hot-path
    one) must consciously touch this contract.
(e) **No Rust surface** — the ``fno_math`` registered-symbol surface stays
    tier-free.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
from pathlib import Path
from typing import get_args, get_type_hints

import brutex
import pytest
from brutex.ranking import tiers
from brutex.ranking.tiers import (
    MISSING_INPUT_PREFIX,
    TIER_LABELS,
    TIER_REASON_ORDER,
    TierInputs,
    TierVerdict,
)

_TIERS_SOURCE: Path = Path(tiers.__file__)
_PKG_ROOT: Path = Path(brutex.__file__).parent
_REPO_ROOT: Path = Path(brutex.__file__).parents[1]

#: The frozen top-level module names tiers.py may import — stdlib ONLY
#: (the whole point: no wheel, no facade, no brutex.math edge).
_FROZEN_IMPORT_ROOTS: frozenset[str] = frozenset({"__future__", "math", "dataclasses", "typing"})

#: The frozen 13-token reason vocabulary in evaluation order (message bytes).
_FROZEN_REASON_ORDER: tuple[str, ...] = (
    "non_finite_input",
    "min_trades",
    "win_rate",
    "payoff",
    "profit_factor",
    "expectancy_headroom",
    "stop_missing",
    "stop_too_wide",
    "stop_not_contained",
    "max_drawdown",
    "minute_path_drawdown",
    "consecutive_losses",
    "cascade",
)

#: The frozen TierInputs field ORDER (dataclass field positions).
_FROZEN_INPUT_FIELDS: tuple[str, ...] = (
    "total_trades",
    "win_rate",
    "win_loss_ratio",
    "losing_trades",
    "profit_factor",
    "gross_loss_paisa",
    "expectancy_paisa",
    "sl_paisa",
    "entry_price_paisa",
    "mae_1m_worst_paisa",
    "trades_with_1m",
    "equity_status",
    "max_drawdown_pct",
    "max_dd_minute_path_pct",
    "max_consecutive_losses",
    "cascade_status",
)

#: The frozen Optional-field set — the load-bearing ``None`` taxonomy
#: (zero-losers payoff pass, zero-gross-loss PF pass, absent stop arm,
#: unusable entry price, absent minute path, unmeasured minute DD, unknown
#: streak, absent cascade verdict) any kernel boundary would have to encode.
_FROZEN_OPTIONAL_INPUTS: frozenset[str] = frozenset(
    {
        "win_loss_ratio",
        "profit_factor",
        "sl_paisa",
        "entry_price_paisa",
        "mae_1m_worst_paisa",
        "max_dd_minute_path_pct",
        "max_consecutive_losses",
        "cascade_status",
    }
)

#: The frozen production-consumer set (paths relative to the ``brutex``
#: package root). ``ranking/__init__.py`` deliberately does NOT re-export
#: tiers (verified at freeze time), so the whole production surface is the
#: run-end artifact writer + the display-side label import.
_FROZEN_CONSUMERS: frozenset[str] = frozenset(
    {
        "engine/top100_artifact.py",
        "dashboard/oneclick_artifacts.py",
    }
)


def _module_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    """The dotted module names an import node references."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return [node.module] if node.module is not None else []


class TestModuleBoundaryFrozen:
    """(a) tiers.py stays a stdlib-pure leaf with the verdict recorded."""

    def test_imports_are_stdlib_only_at_every_scope(self) -> None:
        tree = ast.parse(_TIERS_SOURCE.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, (
                    "tiers.py must carry NO relative imports (leaf law); "
                    f"found level={node.level} at line {node.lineno}"
                )
            if isinstance(node, ast.Import | ast.ImportFrom):
                for name in _module_names(node):
                    roots.add(name.split(".")[0])
        assert roots == set(_FROZEN_IMPORT_ROOTS)

    def test_no_dynamic_import_or_wheel_reference(self) -> None:
        source = _TIERS_SOURCE.read_text(encoding="utf-8")
        for token in ("fno_math", "importlib", "__import__", "brutex.math"):
            assert token not in source, (
                f"tiers.py must not reference {token!r} — the NO-GO verdict "
                "(DEC-RANK-TIERS-RUST-NOGO-001) pins the module wheel-free"
            )

    def test_no_rust_named_module_attribute(self) -> None:
        rust_named = [name for name in dir(tiers) if "RUST" in name.upper()]
        assert not rust_named

    def test_docstring_records_the_verdict(self) -> None:
        doc = tiers.__doc__
        assert doc is not None
        assert "DEC-RANK-TIERS-RUST-NOGO-001" in doc
        assert "expected payoff ≈ 0 — conversion is for uniformity, not speed" in doc


class TestReasonVocabularyFrozen:
    """(b) the reason strings are frozen message bytes."""

    def test_reason_order_is_the_frozen_tuple(self) -> None:
        assert TIER_REASON_ORDER == _FROZEN_REASON_ORDER

    def test_reason_tokens_are_wellformed(self) -> None:
        assert len(TIER_REASON_ORDER) == 13
        assert len(set(TIER_REASON_ORDER)) == len(TIER_REASON_ORDER)
        for token in TIER_REASON_ORDER:
            assert isinstance(token, str)
            assert token

    def test_missing_input_prefix_is_the_frozen_literal(self) -> None:
        assert MISSING_INPUT_PREFIX == "missing_input:"
        assert MISSING_INPUT_PREFIX.startswith("missing_input")
        assert MISSING_INPUT_PREFIX.endswith(":")

    def test_tier_labels_are_the_frozen_tuple(self) -> None:
        assert TIER_LABELS == ("S+", "S", "A")


class TestDecisionSurfacePythonShaped:
    """(c) the classifier's I/O surface is Python-shaped and frozen."""

    def test_tier_inputs_field_order_frozen(self) -> None:
        names = tuple(f.name for f in dataclasses.fields(TierInputs))
        assert names == _FROZEN_INPUT_FIELDS

    def test_tier_inputs_optional_taxonomy_frozen(self) -> None:
        hints = get_type_hints(TierInputs)
        optional = frozenset(
            name for name in _FROZEN_INPUT_FIELDS if type(None) in get_args(hints[name])
        )
        assert optional == _FROZEN_OPTIONAL_INPUTS

    def test_tier_verdict_surface_frozen(self) -> None:
        names = tuple(f.name for f in dataclasses.fields(TierVerdict))
        assert names == ("tier", "blocked_next")
        hints = get_type_hints(TierVerdict)
        assert set(get_args(hints["tier"])) == {str, type(None)}
        assert get_args(hints["blocked_next"]) == (str, Ellipsis)


def _imports_tiers(path: Path) -> bool:
    """Whether *path* imports ``brutex.ranking.tiers`` (absolute OR relative,
    directly or via ``from brutex.ranking import tiers``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # The importing module's own package parts (for relative resolution):
    # brutex/<sub>/<file>.py lives in package ("brutex", <sub...>).
    pkg_parts: tuple[str, ...] = ("brutex", *path.relative_to(_PKG_ROOT).parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "brutex.ranking.tiers" for alias in node.names):
                return True
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if node.level > 0:
            keep = len(pkg_parts) - (node.level - 1)
            if keep < 0:
                continue  # beyond-root relative import — cannot be tiers
            base = pkg_parts[:keep]
            full = ".".join((*base, module)) if module else ".".join(base)
        else:
            full = module
        if full == "brutex.ranking.tiers":
            return True
        if full == "brutex.ranking" and any(a.name == "tiers" for a in node.names):
            return True
    return False


class TestColdPathConsumersFrozen:
    """(d) the frozen production-importer set.

    The NO-GO's cardinality claim — classify_tier runs at most once per
    emitted top-100 entry at run-end artifact build, NEVER on the mask hot
    path — is held STRUCTURALLY by freezing WHO may import the module. A
    NEW consumer (especially a hot-path one) must consciously touch this
    contract, the docstring note and the ledger entry together.
    """

    def test_production_importers_are_exactly_the_frozen_set(self) -> None:
        importers: set[str] = set()
        for path in sorted(_PKG_ROOT.rglob("*.py")):
            rel = path.relative_to(_PKG_ROOT).as_posix()
            if rel == "ranking/tiers.py":
                continue  # the module itself
            if _imports_tiers(path):
                importers.add(rel)
        assert importers == set(_FROZEN_CONSUMERS)


class TestNoRustSurfaceExists:
    """(e) the fno_math registration surface stays tier-free."""

    def test_expected_build_symbols_carry_no_tier_kernel(self) -> None:
        expected_build = (
            _REPO_ROOT / "rust" / "fno-math" / "python" / "fno_math" / "EXPECTED_BUILD.json"
        )
        if not expected_build.is_file():
            pytest.skip(
                f"{expected_build} absent in this checkout — the tier-free "
                "registration pin needs the fno-math shim tree"
            )
        payload: dict[str, object] = json.loads(expected_build.read_text(encoding="utf-8"))
        symbols = payload["symbols"]
        assert isinstance(symbols, list)
        assert symbols, "EXPECTED_BUILD.json symbols list must be non-empty"
        # Token-boundary match ("tier"/"tiers" as an underscore-delimited
        # word, case-insensitive) — a plain substring scan false-positives
        # on the landed ``rank_pareto_frontier`` ("fron·tier"), which is the
        # W1 Pareto kernel, not a tier-classifier kernel.
        token = re.compile(r"(?:^|_)tiers?(?:$|_)", re.IGNORECASE)
        tiered = [
            name for name in symbols if isinstance(name, str) and token.search(name) is not None
        ]
        assert not tiered, (
            "fno_math registered a tier-named kernel — the "
            "DEC-RANK-TIERS-RUST-NOGO-001 verdict must be consciously "
            f"superseded first: {tiered!r}"
        )
