"""Mutation-lane AGGREGATOR for ``brutex/engine/sweep_core.py`` — no new tests.

The weekly ``mutation.yml`` python loop drives ONE test file per module
(the pair grammar is single-file: ``pytest "$tf"`` per mutant), but
``sweep_core.py`` carries TWO arms with two suites — the Slice-1 enum arm
(``test_engine_sweep_core.py``) and the Slice-2 fold arm
(``test_engine_sweep_core_fold.py``). This module re-exports BOTH suites'
``Test*`` classes / ``test_*`` functions into its own namespace so the
mutant runner executes the union. Re-exported test callables keep their
defining module's globals (helpers resolve normally); the per-test seam
reset is the SUPERSET fixture below (enum shadow + fold burn-in +
observability memo), keeping both suites order-independent here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from brutex.engine.sweep_core import (
    reset_enum_self_check_for_tests,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
)

from tests.unit import test_engine_sweep_core as _enum_suite
from tests.unit import test_engine_sweep_core_fold as _fold_suite


def _adopt_suites() -> None:
    for suite in (_enum_suite, _fold_suite):
        for name in dir(suite):
            if name.startswith(("Test", "test_")):
                globals()[name] = getattr(suite, name)


_adopt_suites()


@pytest.fixture(autouse=True)
def _fresh_seam_state_all() -> Iterator[None]:
    """The superset per-test reset for BOTH aggregated suites."""
    reset_enum_self_check_for_tests()
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    yield
    reset_enum_self_check_for_tests()
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
