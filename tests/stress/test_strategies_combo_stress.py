"""Stress harness for the hardened combo canonicaliser + grid enumerator.

DEC-STRATEGIES-CANONICAL-HARDEN. Two sustained-load proofs the one-shot unit
tests structurally cannot give:

1. **Deep nesting never crashes the interpreter.** A pathological 10,000-level
   nested dict raises a clean :class:`StrategyError` (the depth cap pre-empts
   ``RecursionError`` and any C-stack overflow / segfault).
2. **A huge ``ParamGrid`` stays memory-bounded.** A 10**6-combo grid consumed
   only K combos via :func:`itertools.islice` must allocate ~K combos' worth of
   memory — proof the Cartesian product is lazy (``itertools.product``), never
   fully materialised.
"""

from __future__ import annotations

import itertools
import tracemalloc
from typing import Any

import pytest
from brutex.strategies.combo import canonicalise_params
from brutex.strategies.errors import StrategyError
from brutex.strategies.sweep import ParamGrid

#: All stress tests carry the ``stress`` marker (registered in pyproject.toml) so
#: the nightly ``pytest tests/stress -m stress`` soak selects them (DEC-STRESS-001).
pytestmark = pytest.mark.stress

#: Nesting depth for the interpreter-crash probe — far past the depth cap.
_DEEP_LEVELS = 10_000

#: Combos consumed from the huge lazy grid (K ≪ the 10**6 total).
_CONSUME_K = 100


class TestDeepNestingNoInterpreterCrash:
    def test_deep_nesting_no_interpreter_crash(self) -> None:
        """A 10,000-level nested dict raises a clean StrategyError — the depth
        cap pre-empts RecursionError and any native stack overflow."""
        value: Any = "leaf"
        for _ in range(_DEEP_LEVELS):
            value = {"k": value}
        with pytest.raises(StrategyError) as excinfo:
            canonicalise_params(value)
        assert "nesting deeper" in str(excinfo.value)
        assert not isinstance(excinfo.value, RecursionError)


class TestLargeGridBoundedMemory:
    def test_large_grid_bounded_memory(self) -> None:
        """Iterating only the first K combos of a 10**6-combo grid allocates a
        bounded amount of memory — the product is never fully materialised."""
        grid = ParamGrid.from_dict({"a": range(1000), "b": range(1000)})
        assert len(grid) == 10**6  # the full product (computed, not materialised)

        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            consumed = list(itertools.islice(iter(grid), _CONSUME_K))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert len(consumed) == _CONSUME_K
        assert consumed[0] == {"a": 0, "b": 0}
        # A fully-materialised 10**6 product would allocate tens of MB; consuming
        # only 100 combos must stay far below a generous 5 MiB ceiling.
        assert peak < 5 * 1024 * 1024, (
            f"peak tracemalloc {peak / 1024 / 1024:.2f} MiB while consuming only "
            f"{_CONSUME_K} of {len(grid)} combos — the grid may be materialising "
            f"the full Cartesian product instead of iterating lazily."
        )
