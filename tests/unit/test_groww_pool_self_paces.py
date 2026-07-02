"""Unit tests for the ``ClientPool`` self-pacing predicate surface.

DEC-RATE-SINGLE-ADMISSION-001 (forensic-audit Wave 1). Stem-matched to
``brutex/adapters/groww/pool.py`` so the strict per-module coverage discovery
gate scopes these tests to the module they cover (moved out of the combined
``test_groww_single_admission.py``). The fakes are imported from the
single-admission suite (COMMON, no copy).
"""

# Shared single-admission fakes imported cross-test-module — sanctioned.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from typing import Any

from brutex.adapters.groww.client import GrowwClient, client_self_paces
from brutex.adapters.groww.pool import ClientPool

from tests.unit.test_groww_single_admission import _FakeSDK


class TestPoolSelfPaces:
    def test_pool_self_paces_only_when_every_leaf_paced(self) -> None:
        sdk: Any = _FakeSDK()
        paced = GrowwClient(sdk=sdk).with_pace(lambda: None)
        unpaced = GrowwClient(sdk=sdk, meter=paced.meter)
        assert ClientPool([paced, paced]).self_paces is True
        assert ClientPool([paced, unpaced]).self_paces is False
        assert client_self_paces(ClientPool([paced, paced])) is True
