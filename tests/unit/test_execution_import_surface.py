"""Import-surface pin for :mod:`brutex.execution` (DEC-EXECUTION-TRIO-DELETE-001).

The deprecated, dormant ``IndianCostModel`` / ``ExecutionSimulator`` /
``BacktestDriver`` trio was deleted as one atomic unit. This suite pins
the surviving surface:

- ``import brutex.execution`` succeeds (the package ``__init__`` no
  longer executes trio module code);
- ``__all__`` re-exports exactly the errors + types surface and no
  longer names any of the three deleted classes;
- the trio submodules are genuinely gone (``ModuleNotFoundError``).
"""

from __future__ import annotations

import importlib

import pytest

_DELETED_CLASSES = ("BacktestDriver", "ExecutionSimulator", "IndianCostModel")

_DELETED_SUBMODULES = (
    "brutex.execution.costs",
    "brutex.execution.driver",
    "brutex.execution.simulator",
)

_EXPECTED_ALL = [
    "BacktestResult",
    "EquityPoint",
    "ExecutionError",
    # DEC-FILL-WORST-CASE-ONLY-001: the single-member fill vocabulary + the
    # removed-knob refusal guard (the resolver/default machinery is DELETED).
    "FILL_MODE_ENV",
    "Fill",
    "FillCosts",
    "FillMode",
    "FillModeRemovedError",
    "FreezeQuantityExceededError",
    "InstrumentMeta",
    "InsufficientFundsError",
    "InvalidLotSizeError",
    "InvalidPriceError",
    "NonMonotonicBarError",
    "Position",
    "require_fill_mode_env_unset",
]


def test_import_succeeds_and_all_matches_surviving_surface() -> None:
    """``import brutex.execution`` works; ``__all__`` is exactly errors + types."""
    pkg = importlib.import_module("brutex.execution")
    assert sorted(pkg.__all__) == _EXPECTED_ALL
    for name in pkg.__all__:
        assert getattr(pkg, name) is not None


def test_all_no_longer_names_the_deleted_trio() -> None:
    """None of the three deleted classes is re-exported or reachable."""
    pkg = importlib.import_module("brutex.execution")
    for name in _DELETED_CLASSES:
        assert name not in pkg.__all__
        assert not hasattr(pkg, name)


@pytest.mark.parametrize("module_name", _DELETED_SUBMODULES)
def test_trio_submodules_are_gone(module_name: str) -> None:
    """The trio submodules no longer exist on disk."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)
