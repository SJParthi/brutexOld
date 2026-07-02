"""Unit tests for the ``GrowwClient`` self-pacing predicate surface.

DEC-RATE-SINGLE-ADMISSION-001 (forensic-audit Wave 1). Stem-matched to
``brutex/adapters/groww/client.py`` so the strict per-module coverage discovery
gate scopes these tests to the module they cover (moved out of the combined
``test_groww_single_admission.py``, whose stem matches no touched module —
the same relocation discipline as the sibling doctor-live/R1 PRs). The fakes
are imported from the single-admission suite (COMMON, no copy).
"""

from __future__ import annotations

from brutex.adapters.groww.client import GrowwClient, client_self_paces

from tests.unit.test_groww_single_admission import _FakeSDK


class TestSelfPacesPredicate:
    def test_default_client_does_not_self_pace(self) -> None:
        client = GrowwClient(sdk=_FakeSDK())
        assert client.self_paces is False
        assert client_self_paces(client) is False

    def test_bound_pace_flips_self_paces(self) -> None:
        client = GrowwClient(sdk=_FakeSDK()).with_pace(lambda: None)
        assert client.self_paces is True
        assert client_self_paces(client) is True

    def test_objects_without_the_surface_read_false(self) -> None:
        assert client_self_paces(object()) is False
        assert client_self_paces(None) is False
