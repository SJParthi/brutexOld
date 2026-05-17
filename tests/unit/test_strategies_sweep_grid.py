"""Unit tests for :class:`brutex.strategies.sweep.ParamGrid` (pure)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from brutex.strategies.errors import StrategyError
from brutex.strategies.sweep import ParamGrid


class TestParamGrid:
    def test_empty_grid_yields_one_empty_dict(self) -> None:
        grid = ParamGrid.from_dict({})
        out = list(grid)
        assert out == [{}]
        assert len(grid) == 1

    def test_single_axis(self) -> None:
        grid = ParamGrid.from_dict({"fast": [5, 10, 15]})
        out = list(grid)
        assert out == [{"fast": 5}, {"fast": 10}, {"fast": 15}]
        assert len(grid) == 3

    def test_two_axes_cartesian_product(self) -> None:
        grid = ParamGrid.from_dict({"fast": [5, 10], "slow": [20, 50]})
        out = list(grid)
        assert len(out) == 4
        assert len(grid) == 4
        # Every combination appears exactly once.
        combinations = {(d["fast"], d["slow"]) for d in out}
        assert combinations == {(5, 20), (5, 50), (10, 20), (10, 50)}

    def test_three_axes_len_equals_product(self) -> None:
        grid = ParamGrid.from_dict({"a": [1, 2, 3], "b": [1, 2], "c": [1, 2, 3, 4]})
        assert len(grid) == 24
        assert sum(1 for _ in grid) == 24

    def test_key_order_is_lexicographic(self) -> None:
        """Canonical iteration order matches the combo_hash canonicalisation."""
        grid = ParamGrid.from_dict({"z": [1], "a": [1], "m": [1]})
        only = next(iter(grid))
        # The dict insertion order should be alphabetical.
        assert list(only.keys()) == ["a", "m", "z"]

    def test_iteration_is_repeatable(self) -> None:
        grid = ParamGrid.from_dict({"x": [1, 2], "y": [3, 4]})
        run1 = list(grid)
        run2 = list(grid)
        assert run1 == run2

    def test_empty_axis_rejected(self) -> None:
        with pytest.raises(StrategyError, match="at least one"):
            ParamGrid.from_dict({"fast": []})

    def test_iterable_axis_materialised(self) -> None:
        """Axes passed as generators must still be iterable twice."""

        def gen() -> Iterator[int]:
            yield from [1, 2, 3]

        grid = ParamGrid.from_dict({"a": gen()})
        assert len(list(grid)) == 3
        # Second iteration must still work — the original generator
        # would be exhausted, but ParamGrid materialises to tuples.
        assert len(list(grid)) == 3
