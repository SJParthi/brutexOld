"""O(1) bench for the ``brutex.math`` facade — per-symbol lookup latency.

DEC-STRUCTURE-CLEANUP-001 (``docs/MIGRATION.md`` #567).

Honest envelope (Golden Rule #10): building the facade (``_reexport``) is a
**one-time O(total-symbols)** cost at import — NOT O(1)-total. What is O(1) is
the per-symbol *lookup* a caller pays on every use (``math.blake3_hash``): a
module-attribute dict access whose latency does not grow with how many entry
points the facade re-exports. This bench proves that by re-exporting 10 vs 1000
fake symbols and asserting the lookup-latency ratio stays under the ceiling.

The local Python 3.11 sandbox cannot build the real wheels, so this bench drives
fake kernels injected into ``sys.modules`` — the lookup path measured is the
identical ``module.__dict__`` access the real facade uses.
"""

from __future__ import annotations

import importlib
import sys
import time
import types
from typing import Any

import pytest

from tests.bench._o1_tiers import ratio_ceiling

_FACADE = "brutex.math"


def _facade_with_n_symbols(n: int) -> types.ModuleType:
    """Import a fresh ``brutex.math`` re-exporting *n* fake fno_math symbols."""
    fno = types.ModuleType("fno_math")
    names = [f"sym_{i:04d}" for i in range(n)]
    for name in names:
        setattr(fno, name, lambda: None)
    setattr(fno, "__version__", "0")  # noqa: B010 — ModuleType dunder write
    setattr(fno, "__all__", [*names, "__version__"])  # noqa: B010
    trading = types.ModuleType("trading_core")
    setattr(trading, "__version__", "0")  # noqa: B010
    setattr(trading, "__all__", ["__version__"])  # noqa: B010
    sys.modules["fno_math"] = fno
    sys.modules["trading_core"] = trading
    sys.modules.pop(_FACADE, None)
    return importlib.import_module(_FACADE)


@pytest.fixture(autouse=True)
def _restore_modules() -> Any:
    """Restore the kernel + facade module slots after each bench."""
    saved = {k: sys.modules.get(k) for k in ("fno_math", "trading_core", _FACADE)}
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


class TestSymbolLookupIsO1:
    """A facade symbol lookup is O(1) in the number of re-exported symbols."""

    def test_lookup_at_10_symbols(self, benchmark: Any) -> None:
        facade = _facade_with_n_symbols(10)
        result = benchmark(lambda: facade.sym_0005)
        assert callable(result)

    def test_lookup_at_1000_symbols(self, benchmark: Any) -> None:
        facade = _facade_with_n_symbols(1000)
        result = benchmark(lambda: facade.sym_0500)
        assert callable(result)

    def test_lookup_ratio_below_ceiling(self) -> None:
        """Lookup latency at 1000 vs 10 re-exports must stay under the ceiling."""
        iterations = 200_000

        def _time_lookups(n: int, attr: str) -> float:
            facade = _facade_with_n_symbols(n)
            for _ in range(2_000):  # warm up
                getattr(facade, attr)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                getattr(facade, attr)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_10 = _time_lookups(10, "sym_0005")
        t_1000 = _time_lookups(1000, "sym_0500")
        ratio = t_1000 / max(t_10, 1.0)

        assert ratio < ratio_ceiling(5.0), (
            f"facade symbol-lookup latency ratio {ratio:.2f}× (1000 vs 10 "
            f"re-exports) exceeds the 5× CI ceiling — attribute access should "
            f"be O(1). t_10={t_10:.0f}ns, t_1000={t_1000:.0f}ns."
        )
