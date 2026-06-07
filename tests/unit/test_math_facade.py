"""Unit tests for ``brutex.math`` — the Rust hot-kernel facade.

DEC-STRUCTURE-CLEANUP-001 (``docs/MIGRATION.md`` #567). The facade is a thin,
programmatic re-export of the two compiled PyO3 wheels (``fno_math`` +
``trading_core``). The compiled wheels cannot be built on the local
Python 3.11 sandbox, so every test here drives the facade against **fake
kernel modules** injected into ``sys.modules`` before import — wheel-free and
deterministic on both the sandbox and CI (where the real wheels also exist;
the real-wheel surface is pinned by ``tests/golden/test_math_facade_golden.py``).

Coverage target: 100% line + 100% branch on ``brutex/math/__init__.py`` under
the strict per-module CI gate.
"""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

_FACADE = "brutex.math"


# ─────────────────────────────────────────────────────────────────
# Fake-kernel construction
# ─────────────────────────────────────────────────────────────────


def _fake_kernel(
    name: str,
    *,
    symbols: dict[str, Any],
    version: str,
    with_dunder_all: bool = True,
) -> types.ModuleType:
    """Build a stand-in Rust-kernel module.

    Mirrors the real wheels: a set of public callables/constants, a
    ``__version__`` string, and (optionally) a declared ``__all__`` that
    lists the public symbols plus ``__version__``.
    """
    module = types.ModuleType(name)
    for sym_name, value in symbols.items():
        setattr(module, sym_name, value)
    setattr(module, "__version__", version)  # noqa: B010 — ModuleType dunder write
    if with_dunder_all:
        setattr(module, "__all__", [*symbols.keys(), "__version__"])  # noqa: B010
    return module


def _default_kernels() -> tuple[types.ModuleType, types.ModuleType]:
    """A representative (fno_math, trading_core) fake pair with no collision."""
    fno = _fake_kernel(
        "fno_math",
        symbols={
            "blake3_hash": lambda payload: b"\x00" * 32,
            "popcount_bytes": lambda raw: 0,
        },
        version="1.1.1",
    )
    trading = _fake_kernel(
        "trading_core",
        symbols={
            "evaluate_with_levels": lambda *a, **k: 0,
            "version": lambda: "9.9.9",
        },
        version="2.2.2",
    )
    return fno, trading


@pytest.fixture
def fresh_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[types.ModuleType, types.ModuleType, types.ModuleType]]:
    """Import ``brutex.math`` fresh against fake kernels.

    Yields ``(facade, fno_fake, trading_fake)``. Both the fakes and the cached
    facade module are torn down so each test gets a pristine import.
    """
    fno, trading = _default_kernels()
    monkeypatch.setitem(sys.modules, "fno_math", fno)
    monkeypatch.setitem(sys.modules, "trading_core", trading)
    monkeypatch.delitem(sys.modules, _FACADE, raising=False)
    facade = importlib.import_module(_FACADE)
    try:
        yield facade, fno, trading
    finally:
        monkeypatch.delitem(sys.modules, _FACADE, raising=False)


# ─────────────────────────────────────────────────────────────────
# Re-export behaviour (module-level execution)
# ─────────────────────────────────────────────────────────────────


def test_reexports_fno_math_symbols(
    fresh_facade: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """Every fno_math public symbol is re-bound to the SAME object (no copy)."""
    facade, fno, _ = fresh_facade
    assert facade.blake3_hash is fno.blake3_hash
    assert facade.popcount_bytes is fno.popcount_bytes


def test_reexports_trading_core_symbols(
    fresh_facade: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """Every trading_core public symbol is re-bound to the SAME object."""
    facade, _, trading = fresh_facade
    assert facade.evaluate_with_levels is trading.evaluate_with_levels
    assert facade.version is trading.version


def test_all_contains_every_kernel_symbol_plus_helper(
    fresh_facade: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """``__all__`` = sorted(kernel symbols) + the facade's own helper."""
    facade, _, _ = fresh_facade
    assert set(facade.__all__) == {
        "blake3_hash",
        "popcount_bytes",
        "evaluate_with_levels",
        "version",
        "kernel_versions",
    }
    assert facade.__all__[-1] == "kernel_versions"
    # The kernel-derived prefix is sorted (deterministic public surface).
    kernel_part = facade.__all__[:-1]
    assert kernel_part == sorted(kernel_part)


def test_version_attr_is_never_reexported(
    fresh_facade: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """The kernels' ``__version__`` is filtered out (would be ambiguous)."""
    facade, _, _ = fresh_facade
    assert "__version__" not in facade.__all__
    assert getattr(facade, "__version__", None) is None


# ─────────────────────────────────────────────────────────────────
# kernel_versions()
# ─────────────────────────────────────────────────────────────────


def test_kernel_versions_maps_each_kernel(
    fresh_facade: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """Per-kernel provenance is exposed unmerged."""
    facade, _, _ = fresh_facade
    assert facade.kernel_versions() == {"fno_math": "1.1.1", "trading_core": "2.2.2"}


def test_kernel_versions_coerces_to_str(
    fresh_facade: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """A non-str ``__version__`` is coerced (defensive ``str(...)``)."""
    facade, fno, _ = fresh_facade
    setattr(fno, "__version__", 123)  # noqa: B010 — ModuleType dunder write
    versions = facade.kernel_versions()
    assert versions["fno_math"] == "123"


# ─────────────────────────────────────────────────────────────────
# _public_names — the __all__-present / __all__-absent branches
# ─────────────────────────────────────────────────────────────────


def test_public_names_uses_declared_dunder_all(
    fresh_facade: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """When a kernel declares ``__all__`` it is honoured (minus __version__)."""
    facade, _, _ = fresh_facade
    mod = _fake_kernel("k", symbols={"a": 1, "b": 2}, version="0", with_dunder_all=True)
    assert facade._public_names(mod) == ["a", "b"]


def test_public_names_falls_back_to_non_underscore_attrs(
    fresh_facade: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
) -> None:
    """A kernel WITHOUT ``__all__`` exposes its non-underscore attributes."""
    facade, _, _ = fresh_facade
    mod = _fake_kernel("k", symbols={"alpha": 1, "beta": 2}, version="0", with_dunder_all=False)
    # Order follows vars(); compare as a set, __version__ filtered out.
    assert set(facade._public_names(mod)) == {"alpha", "beta"}
    assert "__version__" not in facade._public_names(mod)


# ─────────────────────────────────────────────────────────────────
# _reexport — collision guard + sorted return
# ─────────────────────────────────────────────────────────────────


def test_reexport_raises_on_cross_kernel_collision(
    fresh_facade: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two kernels exporting the same name is a hard RuntimeError, not a shadow."""
    facade, _, _ = fresh_facade
    k1 = _fake_kernel("k1", symbols={"dup": object()}, version="0")
    k2 = _fake_kernel("k2", symbols={"dup": object()}, version="0")
    monkeypatch.setitem(sys.modules, "k1", k1)
    monkeypatch.setitem(sys.modules, "k2", k2)
    with pytest.raises(RuntimeError, match="symbol collision: 'dup'"):
        facade._reexport(("k1", "k2"), {})


def test_reexport_returns_sorted_names_and_binds_namespace(
    fresh_facade: tuple[types.ModuleType, types.ModuleType, types.ModuleType],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The return is sorted and the namespace gains every symbol object."""
    facade, _, _ = fresh_facade
    obj_z = object()
    obj_a = object()
    k = _fake_kernel("ksort", symbols={"zeta": obj_z, "alpha": obj_a}, version="0")
    monkeypatch.setitem(sys.modules, "ksort", k)
    namespace: dict[str, object] = {}
    exported = facade._reexport(("ksort",), namespace)
    assert exported == ["alpha", "zeta"]
    assert namespace["alpha"] is obj_a
    assert namespace["zeta"] is obj_z
