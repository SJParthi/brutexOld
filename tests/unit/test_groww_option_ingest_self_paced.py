"""Unit test for the F&O spine's SINGLE-ADMISSION drop of the window pace.

DEC-RATE-SINGLE-ADMISSION-001 (forensic-audit Wave 1). Stem-matched to
``brutex/adapters/groww/option_ingest.py`` so the strict per-module coverage
discovery gate scopes this test to the branch it covers — a SELF-PACING live
client makes ``ingest_option_contract`` drop the per-window admission (exactly
one admission per physical fetch; pre-fix each fetch was admitted TWICE).
Moved out of the combined ``test_groww_single_admission.py``; the fakes are
imported from it (COMMON, no copy).
"""

# Shared single-admission fakes imported cross-test-module — sanctioned.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import date

from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.option_ingest import ingest_option_contract

from tests.unit.test_groww_single_admission import _Admissions, _ce, _FakeSDK, _Sink


class TestFnoSelfPacedSingleAdmission:
    def test_each_physical_fetch_admitted_exactly_once(self) -> None:
        """Production wiring: pace baked into the client AND passed to the spine (pre-fix 2×)."""
        admissions = _Admissions()
        sdk = _FakeSDK()
        # Mirrors historical_pull: build_live_client bakes controller.pace into the client
        # AND run_groww_option_ingest receives the SAME controller.pace per window.
        client = GrowwClient(sdk=sdk, sleep=lambda _s: None, pace=admissions.pace)
        report = ingest_option_contract(
            client,
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            pace=admissions.pace,
        )
        assert report.bars_appended == 1
        assert sdk.calls == 1
        assert admissions.count == sdk.calls  # exactly ONE admission per fetch (was 2)
