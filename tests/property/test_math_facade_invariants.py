"""Property invariants for the ``brutex.math`` Rust-kernel facade.

DEC-STRUCTURE-CLEANUP-001 (``docs/MIGRATION.md`` #568). Drives the facade's
``_reexport`` / ``_public_names`` against randomly generated fake kernels so the
re-export contract holds for any kernel surface — independent of the compiled
wheels (which cannot build on the local Python 3.11 sandbox).

Invariants
----------
* I-1  Every public kernel symbol is bound into the namespace, to the same object.
* I-2  ``__version__`` is never re-exported.
* I-3  The returned name list is sorted and matches the bound namespace keys.
* I-4  A symbol exported by two kernels raises (no silent shadowing).
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

_FACADE = "brutex.math"

# Valid Python identifiers that never start with an underscore (so they are
# "public") and are never the dunder we filter.
_names = st.from_regex(r"[a-z][a-z0-9_]{0,11}", fullmatch=True).filter(
    lambda s: not s.startswith("_") and s != "__version__"
)


@pytest.fixture(scope="module")
def facade() -> Any:
    """Import the facade against a trivial fake kernel pair (module-scoped).

    Module-scoped so it is compatible with ``@given`` (hypothesis forbids
    re-using a function-scoped fixture across generated examples). The facade's
    ``_reexport`` / ``_public_names`` are pure functions of their arguments, so
    a single stable import is correct for every example.
    """
    saved = {k: sys.modules.get(k) for k in ("fno_math", "trading_core", _FACADE)}
    fno = types.ModuleType("fno_math")
    setattr(fno, "__version__", "0")  # noqa: B010 — ModuleType dunder write
    setattr(fno, "__all__", ["__version__"])  # noqa: B010
    trading = types.ModuleType("trading_core")
    setattr(trading, "__version__", "0")  # noqa: B010
    setattr(trading, "__all__", ["__version__"])  # noqa: B010
    sys.modules["fno_math"] = fno
    sys.modules["trading_core"] = trading
    sys.modules.pop(_FACADE, None)
    mod = importlib.import_module(_FACADE)
    yield mod
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _install(name: str, symbol_names: list[str], *, with_all: bool) -> types.ModuleType:
    mod = types.ModuleType(name)
    for s in symbol_names:
        setattr(mod, s, object())
    setattr(mod, "__version__", "1.0.0")  # noqa: B010 — ModuleType dunder write
    if with_all:
        setattr(mod, "__all__", [*symbol_names, "__version__"])  # noqa: B010
    sys.modules[name] = mod
    return mod


@given(symbol_names=st.lists(_names, unique=True, max_size=8), with_all=st.booleans())
def test_reexport_binds_all_and_sorts(facade: Any, symbol_names: list[str], with_all: bool) -> None:
    """I-1 + I-2 + I-3: all symbols bound (same object), sorted, no __version__."""
    try:
        mod = _install("kproperty", symbol_names, with_all=with_all)
        namespace: dict[str, object] = {}
        exported = facade._reexport(("kproperty",), namespace)

        assert exported == sorted(symbol_names)  # I-3 sorted
        assert set(namespace) == set(symbol_names)  # I-3 keys match
        assert "__version__" not in namespace  # I-2
        for s in symbol_names:
            assert namespace[s] is getattr(mod, s)  # I-1 same object
    finally:
        sys.modules.pop("kproperty", None)


@given(
    shared=_names,
    extra_a=st.lists(_names, unique=True, max_size=4),
    extra_b=st.lists(_names, unique=True, max_size=4),
)
def test_cross_kernel_collision_always_raises(
    facade: Any, shared: str, extra_a: list[str], extra_b: list[str]
) -> None:
    """I-4: a name in both kernels raises RuntimeError regardless of the extras."""
    names_a = [*{*extra_a, shared}]
    names_b = [*{*extra_b, shared}]
    try:
        _install("kcol_a", names_a, with_all=True)
        _install("kcol_b", names_b, with_all=True)
        with pytest.raises(RuntimeError, match="symbol collision"):
            facade._reexport(("kcol_a", "kcol_b"), {})
    finally:
        sys.modules.pop("kcol_a", None)
        sys.modules.pop("kcol_b", None)
